"""Views dos dashboards.

Todas exigem login + membership ativa. Tenant é resolvido pelo middleware
e via context_processor exposto em `current_organization`.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, F
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
)
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.analytics.application.aggregations import (
    atendimento_hora_esperado,
    atendimento_lista_queryset,
    compute_aging_distribution,
    compute_arpu_by_plan,
    compute_at_risk_contracts,
    compute_atendimento_conversao,
    compute_atendimento_detail,
    compute_atendimento_eventos_rede,
    compute_atendimento_horario,
    compute_atendimento_lista,
    compute_atendimento_lista_por_hora,
    compute_atendimento_tendencias,
    compute_atendimento_triagem,
    compute_bad_conversations,
    compute_bandwidth_summary,
    compute_blocked_at_risk_summary,
    compute_blocked_duration_distribution,
    compute_bot_deflection_trend,
    compute_burn_rate,
    compute_cash_calendar,
    compute_cash_received_series,
    compute_cashflow_series,
    compute_churn_by_plan,
    compute_churn_by_reason,
    compute_churn_plan_detail,
    compute_churn_risk_summary,
    compute_churn_summary,
    compute_compromissos_futuros,
    compute_contract_kpi_trend,
    compute_contract_status_trend,
    compute_cto_summary,
    compute_customer_360,
    compute_delinquency_trend,
    compute_dre,
    compute_dre_by_account,
    compute_equipment_field_trend,
    compute_equipment_summary,
    compute_expense_anomalies,
    compute_expense_by_category,
    compute_expense_by_supplier,
    compute_expense_series,
    compute_kpis,
    compute_lead_origin,
    compute_ltv_distribution,
    compute_mao_de_obra_detail,
    compute_mrr_churn_series,
    compute_mrr_series,
    compute_net_adds_series,
    compute_offline_active_customers,
    compute_people_expenses,
    compute_pipeline_aging,
    compute_pipeline_by_status,
    compute_priority_customers,
    compute_qa_overview,
    compute_recovery_rate,
    compute_revenue_comparison,
    compute_revenue_forecast,
    compute_sales_funnel,
    compute_support_sla,
    compute_top_delinquent_invoices,
    compute_top_risk_customers,
    iter_atendimento_lista_rows,
    search_customers,
)
from apps.analytics.application.cto_snapshots import compute_cto_history
from apps.analytics.application.network_snapshots import compute_network_history
from apps.shared.context import get_current_organization

from . import charts
from .period import TZ, Period, get_period, set_period_extra_params


def _require_org(request: HttpRequest) -> Any:  # noqa: ARG001 — assinatura uniforme para uso futuro
    org = get_current_organization()
    if org is None:
        return HttpResponseRedirect("/admin/")  # sem org → fallback admin
    return org


@login_required
@never_cache
def home(request: HttpRequest) -> HttpResponse:  # noqa: ARG001
    return HttpResponseRedirect(reverse("dashboards:executive"))


@login_required
@never_cache
def no_access(request: HttpRequest) -> HttpResponse:
    """Fallback quando a membership não tem nenhuma aba liberada (RBAC #65)."""
    return render(request, "dashboards/no_access.html", status=403)


def _fmt_brl(value: float) -> str:
    """Formata float como 'R$ 326.802' (separador de milhar BR, sem centavos)."""
    v = int(round(value))
    formatted = f"{v:,}".replace(",", ".")
    return f"R$ {formatted}"


def _get_months(request: HttpRequest) -> int:
    """Período das páginas de série mensal, agora via componente (#86).

    Continua devolvendo "quantos meses" porque é isso que as agregações mensais
    sabem receber (`compute_*(org, months=N)`); a escolha em si passou a vir da
    barra de período no topo do conteúdo, e não mais do `<select>` da sidebar.
    URLs antigas `?months=N` seguem funcionando (viram `periodo=Nm`).
    """
    return get_period(request, granularity="month").months


def _get_period(request: HttpRequest) -> Period:
    """Período em dias das páginas de atendimento (#75) — ver `period.py`."""
    return get_period(request)


_PERIOD_TZ = TZ


@login_required
@never_cache
def executive(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    kpis = compute_kpis(org)
    mrr_series = compute_mrr_series(org, months=months)
    aging = compute_aging_distribution(org)
    delinquency_trend = compute_delinquency_trend(org, months=months)
    contract_status_trend = compute_contract_status_trend(org, months=months)
    risk_summary = compute_churn_risk_summary(org)

    # Caixa: recebido (realizado) × projetado — entrada estratégica que faltava (#43).
    # paid_date do IXC tem registros futuros (erro de digitação na baixa), então
    # filtramos por mês corrente real em vez de confiar no último item da série.
    cash_series = compute_cash_received_series(org, months=months)
    forecast_data = compute_revenue_forecast(org, months_ahead=3)
    # Ancorar no último mês com dados reais de caixa, não em today.
    # Na virada de mês (ex: 1/jul sem pagamentos de jul) evita mostrar R$0.
    today_month_key = timezone.now().strftime("%Y-%m")
    valid_cash = [c for c in cash_series if c["month"] <= today_month_key]
    current_month_key = valid_cash[-1]["month"] if valid_cash else today_month_key
    cash_realized_recent = valid_cash[-6:]
    cash_this_month = next(
        (c["amount"] for c in cash_series if c["month"] == current_month_key), 0.0
    )
    cash_projected_next = forecast_data[0]["forecast_cash"] if forecast_data else 0.0
    collection_rate_pct = forecast_data[0]["collection_rate_pct"] if forecast_data else 0.0

    # ARPU = MRR ÷ contratos ativos
    arpu = (
        kpis["mrr_now"] / kpis["active_contracts"]
        if kpis["active_contracts"] > 0
        else 0.0
    )

    # Aging alert: 90+ dias e quanto representa do total inadimplente
    over_90 = next((b for b in aging if b["key"] == "OVER_90"), {})
    total_delinquency = sum(b["amount"] for b in aging if b["key"] != "ON_TIME")
    over_90_pct = (
        over_90.get("amount", 0) / total_delinquency * 100
        if total_delinquency > 0
        else 0.0
    )
    aging_alert = over_90.get("amount", 0) > 0 and over_90_pct > 20

    # Última sync bem-sucedida (para timestamp no header)
    from apps.sync.models import SyncJob, SyncStatus
    last_sync_job = (
        SyncJob.objects
        .filter(organization=org, status=SyncStatus.COMPLETED)
        .order_by("-finished_at")
        .first()
    )
    last_sync = last_sync_job.finished_at if last_sync_job else None

    churn_pct_str = f"{kpis['churn_pct']:.1f}%"
    churn_subtitle = f"{kpis['churn_canceled']} cancelados em {kpis['churn_month_label']} (mês fechado)"
    mrr_delta_str = f"{kpis['mrr_delta_pct']:.1f}% vs mês anterior"
    mrr_subtitle = f"{_fmt_brl(kpis['mrr_prev'])} no mês anterior"

    delinquency_subtitle = (
        f"{kpis['delinquency_count']:,} faturas vencidas — mensalidades acumuladas não pagas".replace(",", ".")
    )
    delinquency_pct_str = f"{kpis['delinquency_pct_of_mrr']:.1f}%"
    over_90_value = _fmt_brl(over_90.get("amount", 0))
    over_90_subtitle = (
        f"{over_90.get('count', 0):,} contratos — provável evasão, requer ação de cobrança".replace(",", ".")
    )

    return render(
        request,
        "dashboards/executive.html",
        {
            "kpis": kpis,
            # Valores pré-formatados — evita bug de |add: string+float no template
            "mrr_now_str": _fmt_brl(kpis["mrr_now"]),
            "mrr_subtitle": mrr_subtitle,
            "mrr_delta_str": mrr_delta_str,
            "mrr_delta_positive": kpis["mrr_delta_pct"] >= 0,
            "arpu_str": _fmt_brl(arpu),
            "churn_pct_str": churn_pct_str,
            "churn_subtitle": churn_subtitle,
            "churn_variant": "border-orange-300" if kpis["churn_pct"] > 1.5 else "border-gray-200",
            "delinquency_amount_str": _fmt_brl(kpis["delinquency_amount"]),
            "delinquency_subtitle": delinquency_subtitle,
            "delinquency_pct_str": delinquency_pct_str,
            "over_90": over_90,
            "over_90_pct": over_90_pct,
            "over_90_value": over_90_value,
            "over_90_subtitle": over_90_subtitle,
            "aging_alert": aging_alert,
            "risk_high": risk_summary["high"],
            "risk_medium": risk_summary["medium"],
            "risk_revenue_str": _fmt_brl(risk_summary["revenue_at_risk"]),
            "last_sync": last_sync,
            "mrr_chart_json": charts.mrr_line_chart(mrr_series),
            "aging_chart_json": charts.aging_bar_chart(aging),
            "delinquency_trend_json": charts.delinquency_trend_chart(delinquency_trend),
            "contract_status_json": charts.contract_status_stacked_chart(contract_status_trend),
            # Caixa recebido × projetado (#43)
            "cash_this_month_str": _fmt_brl(cash_this_month),
            "cash_projected_next_str": _fmt_brl(cash_projected_next),
            "collection_rate_str": f"{collection_rate_pct:.0f}%",
            "cash_vs_projected_json": charts.cash_vs_projected_chart(
                cash_realized_recent, forecast_data
            ),
        },
    )


@login_required
@never_cache
def revenue(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    kpis = compute_kpis(org)
    mrr_series = compute_mrr_series(org, months=months)
    arpu_data = compute_arpu_by_plan(org)
    status_trend = compute_contract_status_trend(org, months=months)
    churn_plan = compute_churn_by_plan(org, months=months)
    comparison = compute_revenue_comparison(org)

    # Pré-formata cada card comparativo — evita arit. de string no template.
    def _fmt_metric(value: float, fmt: str) -> str:
        return _fmt_brl(value) if fmt == "brl" else f"{int(round(value)):,}".replace(",", ".")

    comparison_cards = [
        {
            "label": m["label"],
            "current_str": _fmt_metric(m["current"], m["fmt"]),
            "previous_str": _fmt_metric(m["previous"], m["fmt"]),
            "delta_pct": m["delta_pct"],
            "delta_positive": (m["delta_pct"] >= 0) == m["higher_is_better"],
            "delta_abs_str": _fmt_metric(m["delta_abs"], m["fmt"]),
        }
        for m in comparison
    ]

    arpu = (
        kpis["mrr_now"] / kpis["active_contracts"]
        if kpis["active_contracts"] > 0
        else 0.0
    )
    total_revenue = sum(r["revenue"] for r in arpu_data)
    arpu_data_enriched = [
        {**r, "pct": round(r["revenue"] / total_revenue * 100, 1) if total_revenue > 0 else 0.0}
        for r in arpu_data
    ]

    return render(
        request,
        "dashboards/revenue.html",
        {
            "kpis": kpis,
            "arpu_data": arpu_data_enriched,
            "churn_plan": churn_plan,
            "comparison_cards": comparison_cards,
            "mrr_now_str": _fmt_brl(kpis["mrr_now"]),
            "arpu_str": _fmt_brl(arpu),
            "churn_pct_str": f"{kpis['churn_pct']:.1f}%",
            "churn_subtitle": f"{kpis['canceled_this_month']} cancelados · {kpis['new_this_month']} novos este mês",
            "churn_variant": "border-orange-300" if kpis["churn_pct"] > 1.5 else "border-gray-200",
            "mrr_delta_str": f"{kpis['mrr_delta_pct']:.1f}% vs mês anterior",
            "mrr_delta_positive": kpis["mrr_delta_pct"] >= 0,
            "mrr_subtitle": f"{_fmt_brl(kpis['mrr_prev'])} no mês anterior",
            # charts
            "mrr_dual_json": charts.mrr_contracts_dual_axis(mrr_series),
            "status_trend_json": charts.contract_status_stacked_chart(status_trend),
            "arpu_chart_json": charts.arpu_bar_chart(arpu_data),
            "churn_plan_json": charts.churn_by_plan_bar(churn_plan),
        },
    )


@login_required
@never_cache
def cashflow(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    cashflow_data = compute_cashflow_series(org, months=months)
    supplier_data = compute_expense_by_supplier(org, months=months)
    category_data = compute_expense_by_category(org, months=months)

    # Pré-formatados — evita bug de |add: string+float no template
    last = cashflow_data[-1] if cashflow_data else {}
    last_revenue_str = _fmt_brl(last.get("revenue", 0))
    last_expenses_str = _fmt_brl(last.get("expenses", 0))
    last_net_str = _fmt_brl(last.get("net", 0))
    last_cumulative_str = _fmt_brl(last.get("cumulative_net", 0))

    return render(
        request,
        "dashboards/cashflow.html",
        {
            "cashflow_data": cashflow_data,
            "supplier_data": supplier_data,
            "category_data": category_data,
            "last_revenue_str": last_revenue_str,
            "last_expenses_str": last_expenses_str,
            "last_net_str": last_net_str,
            "last_cumulative_str": last_cumulative_str,
            "cashflow_chart_json": charts.cashflow_waterfall(cashflow_data),
            "supplier_chart_json": charts.expense_by_supplier_bar(supplier_data),
            "category_chart_json": charts.expense_by_category_pie(category_data),
        },
    )


@login_required
@never_cache
def forecast(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    historical = compute_mrr_series(org, months=months)
    cash_series = compute_cash_received_series(org, months=months)
    forecast_data = compute_revenue_forecast(org, months_ahead=12)
    dre_data = compute_dre(org, months=months)

    cur = dre_data["current_month"]
    ytd = dre_data["ytd"]

    # Taxa de recebimento do 1º mês projetado — agora varia por mês (tendência OLS)
    collection_rate_pct = forecast_data[0]["collection_rate_pct"] if forecast_data else None

    return render(
        request,
        "dashboards/forecast.html",
        {
            "historical": historical,
            "cash_series": cash_series,
            "forecast_data": forecast_data,
            "dre_summary": cur,
            "ytd": ytd,
            "collection_rate_pct": collection_rate_pct,
            # Pré-formatados — evita bug de |add: string+Decimal no template
            "cur_receita_str": _fmt_brl(cur["receita_bruta"]),
            "cur_despesas_str": _fmt_brl(cur["despesas"]),
            "cur_ebitda_str": _fmt_brl(cur["ebitda"]),
            "cur_margin_str": f"{cur['ebitda_margin_pct']:.1f}%",
            "ytd_receita_str": _fmt_brl(ytd["receita_bruta"]),
            "ytd_despesas_str": _fmt_brl(ytd["despesas"]),
            "ytd_ebitda_str": _fmt_brl(ytd["ebitda"]),
            "forecast_chart_json": charts.forecast_area(historical, forecast_data),
        },
    )


@login_required
@never_cache
def dre(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    dre_data = compute_dre(org, months=months)

    cur = dre_data["current_month"]

    return render(
        request,
        "dashboards/dre.html",
        {
            "dre": dre_data,
            # Pré-formatados — evita bug de |add: string+Decimal no template
            "cur_receita_str": _fmt_brl(cur["receita_bruta"]),
            "cur_recebida_str": _fmt_brl(cur["receita_recebida"]),
            "cur_em_aberto_str": _fmt_brl(cur["receita_em_aberto"]),
            "cur_despesas_str": _fmt_brl(cur["despesas"]),
            "cur_ebitda_str": _fmt_brl(cur["ebitda"]),
            "cur_margin_str": f"{cur['ebitda_margin_pct']:.1f}%",
            "dre_chart_json": charts.dre_grouped_bar(
                dre_data["mrr_series"], dre_data["op_expense_series"]
            ),
        },
    )


@login_required
@never_cache
def burn(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    burn_data = compute_burn_rate(org, months=min(months, 6))
    expense_series = compute_expense_series(org, months=months)

    # Pré-formatados — evita bug de |add: string+float no template
    burn_rate_str = _fmt_brl(burn_data.get("burn_rate", 0))
    trend_pct_str = f"{burn_data.get('trend_pct', 0):.1f}%"
    last_exp = expense_series[-1] if expense_series else {}
    last_expense_str = _fmt_brl(last_exp.get("expenses", 0))

    return render(
        request,
        "dashboards/burn.html",
        {
            "burn": burn_data,
            "expense_series": expense_series,
            "burn_rate_str": burn_rate_str,
            "trend_pct_str": trend_pct_str,
            "last_expense_str": last_expense_str,
            "burn_chart_json": charts.burn_rate_line(
                burn_data["burn_series"], burn_rate=burn_data["burn_rate"]
            ),
        },
    )


@login_required
@never_cache
def financial(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    kpis = compute_kpis(org)
    aging = compute_aging_distribution(org)
    top_delinquent = compute_top_delinquent_invoices(org, limit=50)
    cash_series = compute_cash_received_series(org, months=months)
    delinquency_trend = compute_delinquency_trend(org, months=months)
    status_trend = compute_contract_status_trend(org, months=months)
    recovery = compute_recovery_rate(org)

    # KPI cards extras
    over_90 = next((b for b in aging if b["key"] == "OVER_90"), {})
    at_risk = sum(b["amount"] for b in aging if b["key"] in ("31_60", "61_90"))
    new_del = next((b for b in aging if b["key"] == "0_30"), {})

    # Blocked contracts série isolada (para o gráfico)
    blocked_series = [
        {"month": s["month"], "label": s["label"], "blocked": s["blocked"]}
        for s in status_trend
    ]

    # Inadimplência separada: principal (MRR) vs multa/juros (#41)
    delinquency_principal = sum(s["principal"] for s in delinquency_trend)
    delinquency_late_fee = sum(s["late_fee"] for s in delinquency_trend)

    return render(
        request,
        "dashboards/financial.html",
        {
            "kpis": kpis,
            "aging": aging,
            "top_delinquent": top_delinquent,
            "over_90": over_90,
            "at_risk_amount": at_risk,
            "new_del": new_del,
            "delinquency_amount_str": _fmt_brl(kpis["delinquency_amount"]),
            "delinquency_pct_str": f"{kpis['delinquency_pct_of_mrr']:.1f}%",
            "over_90_value": _fmt_brl(over_90.get("amount", 0)),
            "at_risk_str": _fmt_brl(at_risk),
            "new_del_str": _fmt_brl(new_del.get("amount", 0)),
            "delinquency_subtitle": f"{kpis['delinquency_count']:,} faturas vencidas".replace(",", "."),
            "delinquency_principal_str": _fmt_brl(delinquency_principal),
            "delinquency_late_fee_str": _fmt_brl(delinquency_late_fee),
            "delinquency_has_late_fee": delinquency_late_fee > 0,
            # Recovery Rate
            "recovery": recovery,
            "recovery_pct_str": f"{recovery['pct']:.1f}%",
            "recovery_recovered_str": _fmt_brl(recovery["recovered_amount"]),
            "recovery_delinquent_str": _fmt_brl(recovery["delinquent_amount"]),
            "recovery_subtitle": (
                f"{recovery['recovered_count']:,} de {recovery['delinquent_count']:,} "
                "faturas recuperadas"
            ).replace(",", "."),
            # charts
            "aging_chart_json": charts.aging_bar_chart(aging),
            "delinquency_trend_json": charts.delinquency_trend_chart(delinquency_trend),
            "cash_chart_json": charts.cash_received_chart(cash_series),
            "blocked_series_json": charts.blocked_trend_line(blocked_series),
            "recovery_chart_json": charts.recovery_by_aging_chart(recovery["by_aging"]),
        },
    )


@login_required
@never_cache
def contracts(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    kpis = compute_kpis(org)
    status_trend = compute_contract_status_trend(org, months=months)
    arpu_data = compute_arpu_by_plan(org)
    churn_plan = compute_churn_by_plan(org, months=months)
    blocked_dist = compute_blocked_duration_distribution(org)
    at_risk_summary = compute_blocked_at_risk_summary(org, min_days=30)
    at_risk_list = compute_at_risk_contracts(org, min_days=30, limit=50)
    equipment = compute_equipment_summary(org)
    kpi_trend = compute_contract_kpi_trend(org, months=months)
    equipment_trend = compute_equipment_field_trend(org, months=months)

    arpu = (
        kpis["mrr_now"] / kpis["active_contracts"]
        if kpis["active_contracts"] > 0
        else 0.0
    )

    from apps.sync.models import SyncJob, SyncStatus
    last_sync_job = (
        SyncJob.objects
        .filter(organization=org, status=SyncStatus.COMPLETED)
        .order_by("-finished_at")
        .first()
    )
    last_sync = last_sync_job.finished_at if last_sync_job else None

    return render(
        request,
        "dashboards/contracts.html",
        {
            "kpis": kpis,
            "at_risk_summary": at_risk_summary,
            "at_risk_list": at_risk_list,
            "last_sync": last_sync,
            "arpu_str": _fmt_brl(arpu),
            "churn_pct_str": f"{kpis['churn_pct']:.1f}%",
            "churn_subtitle": f"{kpis['canceled_this_month']} cancelados · {kpis['new_this_month']} novos",
            "churn_variant": "border-orange-300" if kpis["churn_pct"] > 1.5 else "border-gray-200",
            "at_risk_str": str(at_risk_summary["count"]),
            "at_risk_revenue_str": _fmt_brl(at_risk_summary["revenue_at_risk"]),
            "at_risk_subtitle": (
                f"{_fmt_brl(at_risk_summary['revenue_at_risk'])} em risco · "
                f"{at_risk_summary['pct_of_blocked']:.0f}% dos bloqueados"
            ),
            "pipeline_str": str(kpis["awaiting_contracts"]),
            # Equipamentos em comodato
            "equipment": equipment,
            "equipment_value_str": _fmt_brl(equipment["active_value"]),
            "equipment_count_str": str(equipment["active_count"]),
            "equipment_subtitle": (
                f"{equipment['active_count']} em campo · "
                f"ticket médio {_fmt_brl(equipment['avg_value'])}"
            ),
            # charts
            "status_trend_json": charts.contract_status_stacked_chart(status_trend),
            "arpu_chart_json": charts.arpu_bar_chart(arpu_data),
            "churn_plan_json": charts.churn_by_plan_bar(churn_plan),
            "blocked_dist_json": charts.blocked_duration_histogram(blocked_dist),
            # séries temporais (#42)
            "arpu_trend_json": charts.contract_arpu_trend_line(kpi_trend),
            "churn_trend_json": charts.contract_churn_trend_line(kpi_trend),
            "equipment_trend_json": charts.equipment_field_trend_line(equipment_trend),
            "equipment_trend_has_data": bool(equipment_trend and equipment_trend[-1]["count"] > 0),
        },
    )


@login_required
@never_cache
def pessoas(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    data = compute_people_expenses(org, months=months)
    anomalies = compute_expense_anomalies(org, months=months)
    mao_detail = compute_mao_de_obra_detail(org, months=months)

    people = data.get("people", [])
    mao_de_obra = data.get("mao_de_obra", {})
    grand_total = data.get("grand_total", 0.0)

    # Só mostra anomalias de fornecedores rastreados como pessoas
    person_names = {p["name"] for p in people}
    person_names.add(mao_de_obra.get("name", ""))
    people_anomalies = [a for a in anomalies if a["supplier"] in person_names][:10]

    # Pré-formata totais por pessoa para exibição na tabela
    people_enriched = [
        {**p, "total_str": _fmt_brl(p["total"]), "avg_str": _fmt_brl(p["total"] / max(len(p["monthly"]), 1))}
        for p in people
    ]
    mao_total_str = _fmt_brl(mao_de_obra.get("total", 0.0))

    return render(
        request,
        "dashboards/pessoas.html",
        {
            "data": data,
            "people": people_enriched,
            "mao_de_obra": mao_de_obra,
            "mao_detail": mao_detail,
            "mao_total_str": mao_total_str,
            "month_labels": data.get("month_labels", []),
            "grand_total_str": _fmt_brl(grand_total),
            "num_people": len(people),
            "anomalies": people_anomalies,
            "people_chart_json": charts.people_expenses_stacked_bar(data),
            "mao_chart_json": charts.mao_de_obra_stacked_bar(mao_detail),
        },
    )


@login_required
@never_cache
def dre_detalhe(request: HttpRequest) -> HttpResponse:
    import calendar
    import re
    from datetime import date as _d

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    # --- Período via GET params (?from=YYYY-MM&to=YYYY-MM) ---
    _ym_re = re.compile(r"^\d{4}-\d{2}$")
    raw_from = request.GET.get("from", "")
    raw_to = request.GET.get("to", "")
    from_ym = raw_from if _ym_re.match(raw_from) else ""
    to_ym = raw_to if _ym_re.match(raw_to) else ""

    today = _d.today()

    # Defaults para o seletor de data
    def _default_from() -> str:
        y, m = today.year, today.month - 11
        if m <= 0:
            m += 12
            y -= 1
        return f"{y:04d}-{m:02d}"

    selected_from = from_ym or _default_from()
    selected_to = to_ym or today.strftime("%Y-%m")

    # --- Dados ---
    # dre_detalhe tem seu próprio seletor from/to; o global ?months é fallback.
    # Resolve o período, mas o template NÃO inclui `_period_header.html` (#86):
    # dois controles temporais na mesma tela se contradiriam.
    months = _get_months(request)
    data = compute_dre_by_account(
        org,
        from_ym=from_ym or None,
        to_ym=to_ym or None,
        months=months,
    )
    anomalies = compute_expense_anomalies(org, months=months)

    summary = data.get("summary", {})
    dre_rows = data.get("dre_rows", [])
    month_labels = data.get("month_labels", [])

    total_exp = summary.get("total_expenses", 0.0)
    total_rev = summary.get("total_revenue", 0.0)
    ebitda = summary.get("ebitda", 0.0)
    margin_pct = (ebitda / total_rev * 100) if total_rev > 0 else 0.0

    # Enriquecer dre_rows com monthly_labeled (para template sem zip)
    for row in dre_rows:
        row["monthly_labeled"] = list(zip(month_labels, row["monthly"]))
        if "accounts" in row:
            for acc in row["accounts"]:
                acc["monthly_labeled"] = list(zip(month_labels, acc["monthly"]))
                for sup in acc.get("suppliers", []):
                    sup["monthly_labeled"] = list(zip(month_labels, sup["monthly"]))

    # Opções do seletor — últimos 3 anos, mais recente primeiro
    month_options: list[dict[str, str]] = []
    y, m = today.year, today.month
    for _ in range(37):
        month_options.append({"value": f"{y:04d}-{m:02d}", "label": _d(y, m, 1).strftime("%b/%Y")})
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    month_options.reverse()  # cronológico no <select>

    return render(
        request,
        "dashboards/dre_detalhe.html",
        {
            "data": data,
            "dre_rows": dre_rows,
            "summary": summary,
            "total_expenses_str": _fmt_brl(total_exp),
            "total_revenue_str": _fmt_brl(total_rev),
            "ebitda_str": _fmt_brl(ebitda),
            "ebitda_positive": ebitda >= 0,
            "margin_pct_str": f"{margin_pct:.1f}%",
            "anomalies": anomalies[:15],
            "month_labels": month_labels,
            "month_options": month_options,
            "selected_from": selected_from,
            "selected_to": selected_to,
            "dre_account_chart_json": charts.dre_by_account_stacked_bar(data),
        },
    )


@login_required
@never_cache
def compromissos(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    # Horizonte via ?months_ahead=N (12/24/36); default 24.
    try:
        months_ahead = int(request.GET.get("months_ahead", 24))
    except (ValueError, TypeError):
        months_ahead = 24
    if months_ahead not in (12, 24, 36):
        months_ahead = 24

    data = compute_compromissos_futuros(org, months_ahead=months_ahead)
    months = data["months"]
    month_labels = data["month_labels"]
    summary = data["summary"]

    # Mapa YYYY-MM → 'Mmm/yy' para exibir o mês final de cada frente.
    label_by_month = dict(zip(months, month_labels, strict=False))

    structural = data["structural"]
    for row in structural:
        row["total_str"] = _fmt_brl(row["total"])
        row["last_label"] = label_by_month.get(row["last_month"], row["last_month"] or "—")

    # Escala de alavancagem: dívida total ÷ faturamento mensal (em "meses de
    # faturamento"). Faixas ancoradas em padrão de telecom (Dívida Líq./EBITDA
    # ~3,5× de teto a ~35% de margem ≈ 15 meses de MRR). Quartos de 6/12/18/24.
    mult = summary["divida_multiplo_faturamento"]
    if mult <= 6:
        mult_band, mult_color = "Saudável", "text-green-600"
    elif mult <= 12:
        mult_band, mult_color = "Atenção", "text-yellow-600"
    elif mult <= 18:
        mult_band, mult_color = "Alavancado", "text-orange-600"
    else:
        mult_band, mult_color = "Crítico", "text-red-600"
    # Posição do marcador na barra (0–24× → 0–100%), com teto em 100%.
    mult_marker_pct = min(mult / 24.0, 1.0) * 100.0

    return render(
        request,
        "dashboards/compromissos.html",
        {
            "data": data,
            "structural": structural,
            "summary": summary,
            "months_ahead": months_ahead,
            "total_str": _fmt_brl(summary["total"]),
            "recorrente_str": _fmt_brl(summary["recorrente"]),
            "divida_str": _fmt_brl(summary["divida"]),
            "investimento_str": _fmt_brl(summary["investimento"]),
            "divida_mult_str": (
                f"{summary['divida_multiplo_faturamento']:.1f}×".replace(".", ",")
            ),
            "divida_mult_band": mult_band,
            "divida_mult_color": mult_color,
            "divida_mult_marker_pct": round(mult_marker_pct, 1),
            "faturamento_mensal_str": _fmt_brl(summary["faturamento_mensal"]),
            "divida_last_label": label_by_month.get(
                summary["divida_last_month"], "—"
            ),
            "investimento_last_label": label_by_month.get(
                summary["investimento_last_month"], "—"
            ),
            "compromissos_chart_json": charts.compromissos_futuros_stacked_bar(data),
        },
    )


_MESES_PT = [
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def _cash_calendar_ctx(
    data: dict[str, Any],
    *,
    key: str,
    tab: str,
    subtitle: str,
    in_word: str,
    out_word: str,
) -> dict[str, Any]:
    """Monta o contexto formatado de uma visão (mês) do descasamento."""
    s = data["summary"]
    nd = s["num_days"]

    # Descasamento em dias: positivo = entra DEPOIS de sair (aperto); ~0 alinhado.
    desc = s["descasamento_dias"]
    if abs(desc) < 1.0:
        desc_band, desc_color = "Alinhado", "text-gray-900"
    elif desc > 0:
        desc_band, desc_color = "Entra após sair", "text-red-600"
    else:
        desc_band, desc_color = "Entra antes de sair", "text-green-600"

    be = s["breakeven_day"]
    breakeven_str = f"dia {be}" if be else "não vira positivo"

    return {
        "key": key,
        "tab": tab,
        "subtitle": subtitle,
        "month_label": f"{_MESES_PT[s['month']]}/{s['year']}",
        "chart_div_id": f"descasamento-chart-{key}",
        "data_id": f"descasamento-data-{key}",
        "avg_day_in_str": f"dia {s['avg_day_in']:.0f}" if s["avg_day_in"] else "—",
        "avg_day_out_str": f"dia {s['avg_day_out']:.0f}" if s["avg_day_out"] else "—",
        "in_subtitle": _fmt_brl(s["total_in"]) + f" {in_word}",
        "out_subtitle": _fmt_brl(s["total_out"]) + f" {out_word}",
        "descasamento_str": f"{desc:+.1f}".replace(".", ",") + " dias",
        "descasamento_band": desc_band,
        "descasamento_color": desc_color,
        "worst_balance_str": _fmt_brl(s["worst_balance"]),
        "worst_day_str": f"dia {s['worst_day']}" if s["worst_day"] else "—",
        "days_negative_str": f"{s['days_negative']}/{nd}",
        "breakeven_str": breakeven_str,
        "total_in_str": _fmt_brl(s["total_in"]),
        "total_out_str": _fmt_brl(s["total_out"]),
        "net_str": _fmt_brl(s["net"]),
        "net_positive": s["net"] >= 0,
        "in_word": in_word,
        "out_word": out_word,
        "chart_json": charts.cash_mismatch_chart(data),
    }


@login_required
@never_cache
def descasamento(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    today = timezone.now().date()
    py, pm = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    ny, nm = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)

    past = compute_cash_calendar(org, py, pm, "realized")
    current = compute_cash_calendar(org, today.year, today.month, "hybrid")
    future = compute_cash_calendar(org, ny, nm, "planned")

    views = [
        _cash_calendar_ctx(
            current,
            key="atual",
            tab="Mês atual",
            subtitle=(
                "Efetuado até hoje + a vencer daqui pra frente — como o mês "
                "corrente deve fechar."
            ),
            in_word="previsto entrar",
            out_word="previsto sair",
        ),
        _cash_calendar_ctx(
            past,
            key="passado",
            tab="Mês passado",
            subtitle=(
                "Só o que foi efetuado — recebimentos e pagamentos pela data em "
                "que o caixa realmente mexeu."
            ),
            in_word="recebidos",
            out_word="pagos",
        ),
        _cash_calendar_ctx(
            future,
            key="proximo",
            tab="Mês que vem",
            subtitle=(
                "Só o planejado — a receber × a pagar pelas datas de vencimento, "
                "antes de qualquer baixa."
            ),
            in_word="a receber",
            out_word="a pagar",
        ),
    ]

    return render(
        request,
        "dashboards/descasamento.html",
        {"views": views},
    )


@login_required
@never_cache
def churn(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    summary = compute_churn_summary(org)
    mrr_series = compute_mrr_churn_series(org, months=months)
    reasons = compute_churn_by_reason(org, months=months)
    ltv_dist = compute_ltv_distribution(org)
    plan_detail = compute_churn_plan_detail(org, months=months)

    # Derivados para KPI cards
    net_mrr = summary["net_mrr_this_month"]
    logo_churn_pct_str = f"{summary['logo_churn_pct']:.1f}%"
    mrr_lost_str = _fmt_brl(summary["mrr_lost_this_month"])
    mrr_recovered_str = _fmt_brl(summary["mrr_recovered_this_month"])
    net_mrr_str = _fmt_brl(abs(net_mrr))

    # Plano com maior risk_index (acima da média)
    high_risk_plans = [p for p in plan_detail if (p.get("risk_index") or 0) > 1.0]
    top_risk_plan = high_risk_plans[0] if high_risk_plans else (plan_detail[0] if plan_detail else None)
    top_plan = top_risk_plan["plan"] if top_risk_plan else "—"

    # Taxa global (soma dos cancelamentos / total de bases) para o scatter
    total_base = sum(p.get("base", 0) for p in plan_detail)
    total_canceled_plans = sum(p["count"] for p in plan_detail)
    overall_rate = round(total_canceled_plans / total_base * 100, 2) if total_base > 0 else 0.0

    # Percentuais controláveis
    total_mrr_lost = sum(r["mrr_lost"] for r in reasons)
    controllable_pct = (
        round(sum(r["mrr_lost"] for r in reasons if r["controlavel"] is True)
              / total_mrr_lost * 100, 1)
        if total_mrr_lost > 0 else 0.0
    )

    # Tabela: apenas planos com base >= 10 (bases menores distorcem a taxa)
    # Scatter: mesma regra — remove ruído de planos minúsculos
    plan_detail_display = [p for p in plan_detail if p.get("base", 0) >= 10][:30]

    return render(
        request,
        "dashboards/churn.html",
        {
            "summary": summary,
            "plan_detail": plan_detail_display,
            "overall_rate": overall_rate,
            "reasons": reasons,
            "ltv_dist": ltv_dist,
            # Formatados para os KPI cards
            "logo_churn_pct_str": logo_churn_pct_str,
            "logo_churn_variant": "border-orange-300" if summary["logo_churn_pct"] > 1.5 else "border-gray-200",
            "mrr_lost_str": mrr_lost_str,
            "mrr_recovered_str": mrr_recovered_str,
            "net_mrr_str": net_mrr_str,
            "net_mrr_positive": net_mrr >= 0,
            "ltv_avg_str": f"{summary['ltv_avg_months']:.1f} meses",
            "top_plan": top_plan,
            "controllable_pct": controllable_pct,
            "ticket_alert": summary["ticket_alert"],
            "avg_ticket_canceled_str": _fmt_brl(summary["avg_ticket_canceled"]),
            "avg_ticket_active_str": _fmt_brl(summary["avg_ticket_active"]),
            # Charts
            "churn_mrr_json": charts.churn_mrr_waterfall(mrr_series),
            "churn_logo_json": charts.churn_logo_line(mrr_series),
            "churn_reason_json": charts.churn_reason_pareto(reasons),
            "ltv_hist_json": charts.ltv_histogram(ltv_dist),
            "churn_scatter_json": charts.churn_plan_risk_scatter(plan_detail_display, overall_rate),
        },
    )


@login_required
@never_cache
def operations(request: HttpRequest) -> HttpResponse:
    from apps.helpdesk.infrastructure.models import Ticket

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    # Período em dias (#100). Antes desta issue o seletor desta página governava
    # só o gráfico de volume: os KPIs eram do MÊS CORRENTE hardcoded e o SLA por
    # tipo era fixo em 30 dias. Agora tudo o que tem janela segue o filtro.
    period = _get_period(request)

    # All tickets for this org (TenantManager filters by org)
    qs = Ticket.objects.filter(organization=org)
    now = timezone.now()

    # KPIs. "Chamados abertos" é ESTOQUE (posição de agora) e não tem janela —
    # a página marca isso na UI em vez de fingir que o filtro se aplica.
    open_count = qs.exclude(status="CLOSED").count()

    closed_period_qs = qs.filter(
        status="CLOSED", closed_at__gte=period.start, closed_at__lte=period.end
    )
    closed_in_period = closed_period_qs.count()

    # Tempo médio de resolução dos chamados fechados na janela.
    avg_resolution = closed_period_qs.filter(
        opened_at__isnull=False,
        closed_at__isnull=False,
    ).aggregate(
        avg_hours=Avg(F("closed_at") - F("opened_at"))
    )["avg_hours"]
    avg_resolution_hours = 0.0
    if avg_resolution is not None:
        avg_resolution_hours = avg_resolution.total_seconds() / 3600

    # SLA % (fechados em até 24h / total fechado na janela)
    sla_threshold = timedelta(hours=24)
    if closed_in_period > 0:
        from django.db.models import DurationField, ExpressionWrapper
        within_sla = (
            closed_period_qs
            .filter(opened_at__isnull=False, closed_at__isnull=False)
            .annotate(
                resolution_time=ExpressionWrapper(
                    F("closed_at") - F("opened_at"),
                    output_field=DurationField(),
                )
            )
            .filter(resolution_time__lte=sla_threshold)
            .count()
        )
        sla_pct = round(within_sla / closed_in_period * 100, 1)
    else:
        sla_pct = 0.0

    # Volume abertos x fechados — continua MENSAL. Abaixo de 2 meses de janela
    # sairia um ponto só, então some e a página explica no lugar (#100).
    volume_meses = period.months
    volume_series: list[dict[str, Any]] | None = None
    periodo_nota = ""
    if volume_meses >= 2:
        volume_series = []
        for i in range(volume_meses):
            m_start = (now - relativedelta(months=volume_meses - 1 - i)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            if i < volume_meses - 1:
                m_end = (now - relativedelta(months=volume_meses - 2 - i)).replace(
                    day=1, hour=0, minute=0, second=0, microsecond=0
                )
            else:
                m_end = now
            opened_m = qs.filter(opened_at__gte=m_start, opened_at__lt=m_end).count()
            closed_m = qs.filter(closed_at__gte=m_start, closed_at__lt=m_end).count()
            volume_series.append({
                "month": m_start.strftime("%Y-%m"),
                "label": m_start.strftime("%b/%y"),
                "opened": opened_m,
                "closed": closed_m,
            })
    else:
        periodo_nota = (
            "O volume mês a mês precisa de pelo menos dois meses de janela — "
            "com o período escolhido ele sairia com um ponto só, então está "
            "oculto. O resto da página respeita o período normalmente."
        )

    # Priority distribution (open tickets)
    priority_labels = {
        "URGENT": "Urgente",
        "HIGH": "Alta",
        "NORMAL": "Normal",
        "LOW": "Baixa",
        "UNKNOWN": "Desconhecido",
    }
    priority_qs = (
        qs.exclude(status="CLOSED")
        .values("priority")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    priority_dist = [
        {
            "priority": priority_labels.get(p["priority"], p["priority"]),
            "priority_key": p["priority"],
            "count": p["count"],
        }
        for p in priority_qs
    ]

    # Top 20 open tickets (oldest first)
    open_tickets = list(
        qs.exclude(status="CLOSED")
        .select_related("customer")
        .order_by("opened_at")[:20]
        .values(
            "protocol", "customer__name", "customer_external_id",
            "priority", "status", "opened_at",
        )
    )
    # Enrich with age
    for t in open_tickets:
        if t["opened_at"]:
            delta = now - t["opened_at"]
            t["age_days"] = delta.days
        else:
            t["age_days"] = None
        t["priority_label"] = priority_labels.get(t["priority"], t["priority"])
        status_labels = {
            "OPEN": "Aberto", "SCHEDULED": "Agendado",
            "IN_PROGRESS": "Em execucao", "FORWARDED": "Encaminhado",
        }
        t["status_label"] = status_labels.get(t["status"], t["status"])
        t["customer_name"] = t["customer__name"] or f"Cliente #{t['customer_external_id']}"

    # Format avg resolution
    if avg_resolution_hours >= 24:
        avg_res_str = f"{avg_resolution_hours / 24:.1f} dias"
    else:
        avg_res_str = f"{avg_resolution_hours:.1f}h"

    # SLA por tipo de atendimento (Manutenção/Instalação/...) — na janela do
    # filtro, com comparativo vs a janela anterior de mesma duração. A função já
    # trabalhava em DIAS; até a #100 recebia 30 fixo e ignorava o seletor.
    sla_dias = max(1, (period.end_date - period.start_date).days + 1)
    sla_by_type = compute_support_sla(org, period_days=sla_dias)

    return render(
        request,
        "dashboards/operations.html",
        {
            "open_count": open_count,
            "closed_in_period": closed_in_period,
            "avg_resolution_str": avg_res_str,
            "sla_pct": sla_pct,
            "sla_pct_str": f"{sla_pct:.1f}%",
            "sla_dias": sla_dias,
            "open_tickets": open_tickets,
            "priority_dist": priority_dist,
            "sla_by_type": sla_by_type,
            "periodo_nota": periodo_nota,
            "volume_visivel": volume_series is not None,
            "volume_chart_json": (
                charts.ticket_volume_trend(volume_series) if volume_series else ""
            ),
            "priority_chart_json": charts.ticket_priority_pie(priority_dist),
        },
    )


@login_required
@never_cache
def os_dashboard(request: HttpRequest) -> HttpResponse:
    """Dashboard de Ordens de Serviço — análise por tipo de OS (assunto)."""
    from django.db.models import DurationField, ExpressionWrapper, Q

    from apps.helpdesk.application.os_lookups import load_os_lookups
    from apps.helpdesk.infrastructure.models import Ticket

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    lookups = load_os_lookups(org)
    now = timezone.now()
    window_start = (now - relativedelta(months=months)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # OS abertas dentro da janela do período selecionado.
    qs = Ticket.objects.filter(organization=org, opened_at__gte=window_start)

    # --- KPIs ---
    total_os = qs.count()
    closed_os = qs.filter(status="CLOSED").count()
    solution_rate = round(closed_os / total_os * 100, 1) if total_os else 0.0
    distinct_types = qs.exclude(subject_id="").values("subject_id").distinct().count()

    avg_resolution = qs.filter(
        status="CLOSED", opened_at__isnull=False, closed_at__isnull=False,
    ).aggregate(avg=Avg(F("closed_at") - F("opened_at")))["avg"]
    avg_resolution_hours = avg_resolution.total_seconds() / 3600 if avg_resolution else 0.0
    if avg_resolution_hours >= 24:
        avg_res_str = f"{avg_resolution_hours / 24:.1f} dias"
    else:
        avg_res_str = f"{avg_resolution_hours:.1f}h"

    # --- Agregação por tipo de OS (subject_id → assunto) ---
    by_type = (
        qs.values("subject_id")
        .annotate(
            total=Count("id"),
            closed=Count("id", filter=Q(status="CLOSED")),
            avg_res=Avg(
                ExpressionWrapper(
                    F("closed_at") - F("opened_at"),
                    output_field=DurationField(),
                ),
                filter=Q(
                    status="CLOSED",
                    opened_at__isnull=False,
                    closed_at__isnull=False,
                ),
            ),
        )
        .order_by("-total")
    )

    type_rows = []
    for row in by_type:
        avg_res = row["avg_res"]
        avg_hours = avg_res.total_seconds() / 3600 if avg_res else 0.0
        if avg_hours >= 24:
            row_avg_str = f"{avg_hours / 24:.1f} dias"
        elif avg_hours > 0:
            row_avg_str = f"{avg_hours:.1f}h"
        else:
            row_avg_str = "—"
        row_total = row["total"]
        type_rows.append({
            "subject": lookups.subject_name(row["subject_id"]),
            "subject_id": row["subject_id"],
            "total": row_total,
            "closed": row["closed"],
            "open": row_total - row["closed"],
            "solution_rate": round(row["closed"] / row_total * 100, 1) if row_total else 0.0,
            "avg_res_hours": avg_hours,
            "avg_res_str": row_avg_str,
            "pct_of_total": round(row_total / total_os * 100, 1) if total_os else 0.0,
        })

    # Top 12 tipos por volume — pros gráficos (a tabela mostra todos).
    top_types = type_rows[:12]

    # --- Tendência mensal de OS abertas ---
    trend_series = []
    for i in range(months):
        m_start = (now - relativedelta(months=months - 1 - i)).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        if i < months - 1:
            m_end = (now - relativedelta(months=months - 2 - i)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
        else:
            m_end = now
        opened_m = Ticket.objects.filter(
            organization=org, opened_at__gte=m_start, opened_at__lt=m_end
        ).count()
        trend_series.append({
            "month": m_start.strftime("%Y-%m"),
            "label": m_start.strftime("%b/%y"),
            "opened": opened_m,
        })

    # --- Distribuição por status ---
    status_labels = {
        "OPEN": "Aberto",
        "SCHEDULED": "Agendado",
        "IN_PROGRESS": "Em execução",
        "CLOSED": "Fechado",
        "FORWARDED": "Encaminhado",
        "UNKNOWN": "Desconhecido",
    }
    status_qs = qs.values("status").annotate(count=Count("id")).order_by("-count")
    status_dist = [
        {
            "status": status_labels.get(s["status"], s["status"]),
            "status_key": s["status"],
            "count": s["count"],
        }
        for s in status_qs
    ]

    return render(
        request,
        "dashboards/os.html",
        {
            "total_os": total_os,
            "distinct_types": distinct_types,
            "solution_rate": solution_rate,
            "solution_rate_str": f"{solution_rate:.1f}%",
            "avg_resolution_str": avg_res_str,
            "type_rows": type_rows,
            "synced": bool(lookups.subject_map),
            "volume_chart_json": charts.os_volume_by_type(top_types),
            "resolution_chart_json": charts.os_avg_resolution_by_type(top_types),
            "trend_chart_json": charts.os_monthly_trend(trend_series),
            "status_chart_json": charts.os_status_pie(status_dist),
        },
    )


@login_required
@never_cache
def atendimento(request: HttpRequest) -> HttpResponse:
    """Triagem de atendimentos Opa! Suite por departamento (issue #48).

    Período em DIAS (#89): a página saiu do grupo de presets mensais e passou a
    usar o mesmo componente das telas de atendimento (Hoje/Ontem/7d/…/12m +
    personalizado) — "só o dia de hoje" era impossível com `?months=N`. A URL
    antiga `?months=N` continua valendo: `get_period` a converte em `Nm`.

    Os gráficos de departamento e de motivos são drill-downs (#89): o clique cai
    em `atendimento_lista` com o MESMO período e o identificador da barra, e a
    contagem bate porque as duas pontas usam `atendimento_lista_queryset`.
    """
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    period = _get_period(request)

    departamento_id: int | None = None
    raw_dep = request.GET.get("departamento", "")
    if raw_dep.isdigit():
        departamento_id = int(raw_dep)

    # O form de período personalizado tem que devolver o mesmo recorte (#86).
    set_period_extra_params(request, {"departamento": departamento_id})

    data = compute_atendimento_triagem(
        org, start=period.start, end=period.end, departamento_id=departamento_id
    )
    deflection = compute_bot_deflection_trend(
        org, start=period.start, end=period.end
    )

    # Querystring que o drill-down leva pra lista: período + `foco` (esta página
    # não tem recorte de foco, então é sempre "todos" — mas explícito, porque o
    # default da lista pode mudar) + a origem, pro botão "voltar" trazer de volta
    # a esta tela com o mesmo período.
    drill_query = f"{period.query}&foco=todos&origem=atendimento"
    # No gráfico de motivos o departamento não vem da barra: quando a página está
    # filtrada, ele tem que viajar junto, senão a lista abriria mais gente do que
    # a barra contou.
    motivo_drill_query = drill_query
    if departamento_id is not None:
        motivo_drill_query += f"&departamento={departamento_id}"

    return render(
        request,
        "dashboards/atendimento.html",
        {
            **data,
            **deflection,
            "drill_query": drill_query,
            "motivo_drill_query": motivo_drill_query,
            "volume_chart_json": charts.atendimento_volume_by_departamento(
                data["by_departamento"]
            ),
            "status_chart_json": charts.atendimento_status_pie(data["status_dist"]),
            "trend_chart_json": charts.atendimento_volume_trend(data["trend"]),
            "motivos_chart_json": charts.atendimento_top_motivos(data["top_motivos"]),
            "deflection_chart_json": charts.bot_deflection_trend(
                deflection["deflection_trend"]
            ),
        },
    )


@login_required
@never_cache
def atendimento_tendencias(request: HttpRequest) -> HttpResponse:
    """Tendências de atendimento: motivos e tags ao longo do tempo (F2).

    Período único da página (#75): resolvido uma vez em `_get_period` e passado
    igual pra todos os gráficos — nenhum bloco lê `?months=`/`?hd=` por conta
    própria (esses parâmetros só sobrevivem como compatibilidade de URL antiga).
    """
    from apps.atendimento.infrastructure.models import EventoRede

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    period = _get_period(request)

    granularity = request.GET.get("g", "week")
    if granularity not in ("week", "month"):
        granularity = "week"

    departamento_id: int | None = None
    raw_dep = request.GET.get("departamento", "")
    if raw_dep.isdigit():
        departamento_id = int(raw_dep)

    focus_motivo = request.GET.get("motivo", "").strip() or None
    focus_tag = request.GET.get("tag", "").strip() or None

    # Gráfico horário sazonal (F3) — mesma janela da página; só o foco
    # (?foco=todos|suporte|rede|comercial) é próprio dele (padrão: suporte).
    foco = request.GET.get("foco", "suporte")
    if foco not in ("todos", "suporte", "rede", "comercial"):
        foco = "suporte"

    # Recorte da página que o form de período personalizado precisa preservar
    # (#86) — antes eram hidden inputs escritos à mão no template.
    set_period_extra_params(
        request,
        {
            "g": granularity,
            "foco": foco,
            "departamento": departamento_id,
            "motivo": focus_motivo,
            "tag": focus_tag,
        },
    )

    # `departamento_id` entra também aqui (#77): o clique num ponto abre a lista
    # da hora com o mesmo recorte, então gráfico e lista precisam do MESMO
    # filtro. (Na prática só muda algo no foco "todos" — nos demais o próprio
    # foco define o recorte; ver `atendimento_foco_queryset`.)
    horario = compute_atendimento_horario(
        org,
        start=period.start,
        end=period.end,
        foco=foco,
        departamento_id=departamento_id,
    )

    # Eventos de rede (#78): só os que intersectam a janela EXIBIDA do gráfico
    # (não o período bruto) — a view carrega, o chart só desenha.
    eventos = compute_atendimento_eventos_rede(
        org,
        window_start=horario["window_start"],
        window_end=horario["window_end"],
    )
    membership = request.user.get_active_membership()
    pode_editar_eventos = bool(membership and membership.is_owner)
    # Querystring que o POST devolve pra voltar na MESMA visão (período, foco,
    # granularidade e departamento).
    evento_voltar_query = f"{period.query}&g={granularity}&foco={foco}"
    if departamento_id is not None:
        evento_voltar_query += f"&departamento={departamento_id}"

    data = compute_atendimento_tendencias(
        org,
        start=period.start,
        end=period.end,
        granularity=granularity,
        departamento_id=departamento_id,
        top_n=30,
        focus_motivo=focus_motivo,
        focus_tag=focus_tag,
    )

    # Na visão focada, o "Total" do bucket é o próprio valor da categoria.
    motivo_focus_json = None
    if data["motivo_focus"]:
        motivo_focus_json = charts.atendimento_categoria_trend(
            data["buckets"],
            [data["motivo_focus"]],
            bucket_totals=data["motivo_focus"]["values"],
        )
    tag_focus_json = None
    if data["tag_focus"]:
        tag_focus_json = charts.atendimento_categoria_trend(
            data["buckets"],
            [data["tag_focus"]],
            bucket_totals=data["tag_focus"]["values"],
        )

    return render(
        request,
        "dashboards/atendimento_tendencias.html",
        {
            **data,
            # As variáveis do filtro de período (`period`, `period_label`,
            # `period_presets`, …) vêm do context processor `period_context`.
            "horario": horario,
            "horario_json": charts.atendimento_horario_sazonal(horario, eventos),
            "eventos_rede": eventos,
            "evento_tipos": EventoRede.Tipo.choices,
            "pode_editar_eventos": pode_editar_eventos,
            "evento_erro": request.GET.get("evento_erro") == "1",
            "evento_default_inicio": timezone.localtime(
                timezone.now(), _PERIOD_TZ
            ).strftime(_EVENTO_DT_FMT),
            "evento_voltar_query": evento_voltar_query,
            "motivos_trend_json": charts.atendimento_categoria_trend(
                data["buckets"],
                data["motivos_series"],
                bucket_totals=data["motivos_bucket_totals"],
            ),
            "tags_trend_json": charts.atendimento_categoria_trend(
                data["buckets"],
                data["tags_series"],
                bucket_totals=data["tags_bucket_totals"],
            ),
            "motivo_focus_json": motivo_focus_json,
            "tag_focus_json": tag_focus_json,
        },
    )


# ---------------------------------------------------------------------------
# Lista de atendimentos (#87) — hora (?h), dia (?d) ou período + recorte
# ---------------------------------------------------------------------------
# Uma página só pros três recortes: antes existia apenas a lista de UMA hora
# (#76), com teto de 500 linhas que truncava em silêncio. Aqui o total é sempre
# o real (count no banco) e a tabela é paginada.
# Formato do `?h=` (ISO local, sem fuso) — o mesmo do `customdata` do gráfico.
_LISTA_HORA_FMT = "%Y-%m-%dT%H:%M"
_LISTA_PER_PAGE = 100
_LISTA_FOCOS = ("todos", "suporte", "rede", "comercial")
_LISTA_FOCO_LABELS = {
    "todos": "Todos",
    "suporte": "Suporte",
    "rede": "Rede",
    "comercial": "Comercial",
}
_LISTA_CSV_HEADER = (
    "Cliente", "Documento", "Horário", "Atendente", "Departamento",
    "Categorias", "Protocolo", "Status",
)
# Páginas de onde o drill-down pode vir — whitelist de NOMES de rota, nunca URL
# crua vinda da querystring (evita open redirect no link de "voltar").
_LISTA_ORIGENS = {
    "tendencias": "dashboards:atendimento_tendencias",
    "atendimento": "dashboards:atendimento",
}
_LISTA_ORIGEM_LABELS = {
    "tendencias": "Tendências de Atendimento",
    "atendimento": "Atendimento",
}


def _parse_hora(raw: str) -> datetime | None:
    """`?h=2026-08-03T14:00` (ISO local) → datetime aware truncado na hora."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=_PERIOD_TZ)
    return timezone.localtime(parsed, _PERIOD_TZ).replace(
        minute=0, second=0, microsecond=0
    )


def _parse_dia(raw: str) -> datetime | None:
    """`?d=2026-08-03` → primeiro instante do dia em America/Sao_Paulo."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return datetime.combine(parsed, time.min, tzinfo=_PERIOD_TZ)


def _lista_csv_response(
    rows: Iterable[dict[str, Any]], filename: str
) -> HttpResponse:
    """CSV pt-BR do recorte INTEIRO: separador `;` + BOM UTF-8 (abre no Excel)."""
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.write("﻿")  # BOM — sem ele o Excel pt-BR erra os acentos
    writer = csv.writer(response, delimiter=";", lineterminator="\r\n")
    writer.writerow(_LISTA_CSV_HEADER)
    for r in rows:
        writer.writerow([
            r["customer_name"],
            r["customer_document"],
            r["opened_at_str"],
            r["atendente_nome"],
            r["departamento_nome"],
            ", ".join(r["categorias"]),
            r["protocol"],
            r["status_label"],
        ])
    return response


def _lista_filtros(request: HttpRequest) -> dict[str, Any]:
    """Recorte pedido na querystring, já validado (`foco`, dep., motivo, tag)."""
    foco = request.GET.get("foco", "todos")
    if foco not in _LISTA_FOCOS:
        foco = "todos"
    raw_dep = request.GET.get("departamento", "")
    origem = request.GET.get("origem", "tendencias")
    return {
        "foco": foco,
        "departamento_id": int(raw_dep) if raw_dep.isdigit() else None,
        "motivo": request.GET.get("motivo", "").strip() or None,
        "tag": request.GET.get("tag", "").strip() or None,
        "origem": origem if origem in _LISTA_ORIGENS else "tendencias",
    }


def _lista_filtro_query(filtros: dict[str, Any]) -> str:
    """Querystring do recorte (sem período nem paginação)."""
    params: list[tuple[str, str]] = [("foco", filtros["foco"])]
    if filtros["departamento_id"] is not None:
        params.append(("departamento", str(filtros["departamento_id"])))
    if filtros["motivo"]:
        params.append(("motivo", filtros["motivo"]))
    if filtros["tag"]:
        params.append(("tag", filtros["tag"]))
    if filtros["origem"] != "tendencias":
        params.append(("origem", filtros["origem"]))
    return urlencode(params)


def _lista_recorte(
    request: HttpRequest, period: Period
) -> tuple[str, datetime, datetime] | None:
    """Janela `[start, end)` do recorte pedido, ou None se `?h=`/`?d=` é inválido.

    Precedência: `?h=` (uma hora) > `?d=` (um dia) > período do componente (#86).
    O fim é sempre EXCLUSIVO — o período do componente termina no último instante
    do dia, daí o microssegundo somado.
    """
    raw_h = request.GET.get("h", "").strip()
    if raw_h:
        hour = _parse_hora(raw_h)
        return None if hour is None else ("hora", hour, hour + timedelta(hours=1))

    raw_d = request.GET.get("d", "").strip()
    if raw_d:
        dia = _parse_dia(raw_d)
        return None if dia is None else ("dia", dia, dia + timedelta(days=1))

    return ("periodo", period.start, period.end + timedelta(microseconds=1))


def _lista_csv_filename(kind: str, start: datetime, end: datetime) -> str:
    if kind == "hora":
        return f"atendimentos_{start.strftime('%Y-%m-%d_%Hh')}.csv"
    if kind == "dia":
        return f"atendimentos_{start.strftime('%Y-%m-%d')}.csv"
    ultimo = (end - timedelta(microseconds=1)).date()
    return f"atendimentos_{start.date().isoformat()}_{ultimo.isoformat()}.csv"


def _lista_base_query(
    kind: str, start: datetime, period: Period, filtro_query: str
) -> str:
    """Querystring completa da visão, menos `page`/`format`.

    Base dos links de paginação e do export. Em hora/dia o período viaja junto:
    é o que o link de voltar reproduz na tela de origem.
    """
    if kind == "hora":
        return f"h={start.strftime(_LISTA_HORA_FMT)}&{filtro_query}&{period.query}"
    if kind == "dia":
        return f"d={start.date().isoformat()}&{filtro_query}&{period.query}"
    return f"{period.query}&{filtro_query}"


def _lista_hora_contexto(
    org: Any, start: datetime, filtros: dict[str, Any], nav_query: str
) -> dict[str, Any]:
    """Extras do recorte de uma hora: baseline do slot + navegação hora a hora.

    `dia_query` (#88) é o "Ver o dia todo": mesmo recorte (foco/departamento/
    motivo/tag) e mesmo período de origem, trocando `?h=` por `?d=`. A hora de
    origem viaja em `origem_h` pra a tela do dia saber pra onde é o caminho de
    volta.
    """
    prev_hour = start - timedelta(hours=1)
    next_hour = start + timedelta(hours=1)
    slot = start.strftime(_LISTA_HORA_FMT)
    return {
        "esperado": atendimento_hora_esperado(
            org,
            hour_start=start,
            foco=filtros["foco"],
            departamento_id=filtros["departamento_id"],
        ),
        "prev_query": f"h={prev_hour.strftime(_LISTA_HORA_FMT)}&{nav_query}",
        "next_query": f"h={next_hour.strftime(_LISTA_HORA_FMT)}&{nav_query}",
        "prev_label": prev_hour.strftime("%d/%m %Hh"),
        "next_label": next_hour.strftime("%d/%m %Hh"),
        "next_is_future": next_hour > timezone.localtime(timezone.now(), _PERIOD_TZ),
        "dia_query": f"d={start.date().isoformat()}&{nav_query}&origem_h={slot}",
        "dia_label": start.strftime("%d/%m/%Y"),
    }


def _lista_origem_hora(
    request: HttpRequest, start: datetime, end: datetime
) -> datetime | None:
    """Hora de origem do drill-down (`?origem_h=`), quando ela é DESTE dia.

    Só serve pro caminho de volta do recorte de dia; uma hora de outro dia (ou
    inválida) é ignorada em silêncio — o breadcrumb some, o resto continua.
    """
    raw = request.GET.get("origem_h", "").strip()
    if not raw:
        return None
    hora = _parse_hora(raw)
    if hora is None or not (start <= hora < end):
        return None
    return hora


def _lista_dia_contexto(
    start: datetime, nav_query: str, origem_hora: datetime | None
) -> dict[str, Any]:
    """Extras do recorte de um dia (#88): navegação dia a dia + volta pra hora.

    Reaproveita as chaves `prev_*`/`next_*` da navegação de hora — o bloco do
    template é o mesmo, só muda o passo. `next_is_future` compara DATAS no fuso
    de São Paulo: o dia corrente é parcial e continua navegável, o seguinte não
    existe ainda.
    """
    prev_dia = start - timedelta(days=1)
    next_dia = start + timedelta(days=1)
    hoje = timezone.localtime(timezone.now(), _PERIOD_TZ).date()
    ctx: dict[str, Any] = {
        "prev_query": f"d={prev_dia.date().isoformat()}&{nav_query}",
        "next_query": f"d={next_dia.date().isoformat()}&{nav_query}",
        "prev_label": prev_dia.strftime("%d/%m"),
        "next_label": next_dia.strftime("%d/%m"),
        "next_is_future": next_dia.date() > hoje,
        "dia_label": start.strftime("%d/%m/%Y"),
        "hora_origem_query": None,
        "hora_origem_label": "",
    }
    if origem_hora is not None:
        slot = origem_hora.strftime(_LISTA_HORA_FMT)
        ctx["hora_origem_query"] = f"h={slot}&{nav_query}"
        ctx["hora_origem_label"] = origem_hora.strftime("%Hh")
    return ctx


def _lista_recorte_label(kind: str, start: datetime, end: datetime) -> str:
    """Complemento do título ("N atendimentos …")."""
    if kind == "hora":
        return (
            f"entre {start.strftime('%H:%M')} e {end.strftime('%H:%M')} "
            f"de {start.strftime('%d/%m/%Y')}"
        )
    if kind == "dia":
        return f"em {start.strftime('%d/%m/%Y')}"
    return "no período"


@login_required
@never_cache
def atendimento_lista(request: HttpRequest) -> HttpResponse:
    """Lista de atendimentos de um recorte — hora, dia ou período (#87).

    Destino único dos drill-downs de atendimento: `?h=` (uma hora do gráfico
    horário), `?d=` (o dia inteiro) ou o período do componente (#86) combinado
    com `foco`/`departamento`/`motivo`/`tag`. A contagem sai do MESMO helper de
    recorte do gráfico (`atendimento_foco_queryset`), então o total bate com a
    barra/ponto clicado.

    Paginação de 100 linhas com total real; o CSV (`?format=csv`) exporta o
    recorte inteiro, não a página visível.
    """
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    period = _get_period(request)
    filtros = _lista_filtros(request)
    filtro_query = _lista_filtro_query(filtros)
    # O form de período personalizado precisa devolver o mesmo recorte (#86).
    set_period_extra_params(
        request,
        {
            "foco": filtros["foco"],
            "departamento": filtros["departamento_id"],
            "motivo": filtros["motivo"],
            "tag": filtros["tag"],
            "origem": filtros["origem"],
        },
    )

    voltar_url = reverse(_LISTA_ORIGENS[filtros["origem"]])
    voltar_query = f"{period.query}&{filtro_query}"

    recorte = _lista_recorte(request, period)
    if recorte is None:
        # `?h=`/`?d=` inválidos não têm lista: volta pra origem com o recorte.
        return HttpResponseRedirect(f"{voltar_url}?{voltar_query}")
    kind, start, end = recorte

    recorte_kwargs = {
        "start": start,
        "end": end,
        "foco": filtros["foco"],
        "departamento_id": filtros["departamento_id"],
        "motivo": filtros["motivo"],
        "tag": filtros["tag"],
    }

    if request.GET.get("format") == "csv":
        qs = atendimento_lista_queryset(org, **recorte_kwargs)
        return _lista_csv_response(
            iter_atendimento_lista_rows(qs), _lista_csv_filename(kind, start, end)
        )

    raw_page = request.GET.get("page", "1")
    page = int(raw_page) if raw_page.isdigit() and raw_page != "0" else 1
    data = compute_atendimento_lista(
        org, **recorte_kwargs, page=page, per_page=_LISTA_PER_PAGE
    )

    base_query = _lista_base_query(kind, start, period, filtro_query)
    nav_query = f"{filtro_query}&{period.query}"
    origem_hora = _lista_origem_hora(request, start, end) if kind == "dia" else None
    if origem_hora is not None:
        # Paginar/exportar o dia não pode perder o caminho de volta pra hora.
        base_query += f"&origem_h={origem_hora.strftime(_LISTA_HORA_FMT)}"

    contexto: dict[str, Any] = {
        **data,
        "lista_kind": kind,
        "recorte_label": _lista_recorte_label(kind, start, end),
        "foco_label": _LISTA_FOCO_LABELS[filtros["foco"]],
        "mostrar_periodo": kind == "periodo",
        "per_page": _LISTA_PER_PAGE,
        "base_query": base_query,
        "csv_query": f"{base_query}&format=csv",
        "voltar_query": voltar_query,
        "voltar_url": voltar_url,
        "voltar_label": _LISTA_ORIGEM_LABELS[filtros["origem"]],
    }

    if kind == "hora":
        # A hora mantém o contexto que a tela de #76/#77 tinha.
        contexto.update(_lista_hora_contexto(org, start, filtros, nav_query))
    elif kind == "dia":
        # Dia (#88): navegação dia a dia + mini-gráfico por hora que fecha o
        # ciclo dia ↔ hora (uma agregação por hora sobre o MESMO recorte).
        contexto.update(_lista_dia_contexto(start, nav_query, origem_hora))
        slots = compute_atendimento_lista_por_hora(org, **recorte_kwargs)
        contexto["horas_json"] = charts.atendimento_dia_por_hora(slots)
        contexto["hora_drill_query"] = nav_query

    return render(request, "dashboards/atendimento_lista.html", contexto)


@login_required
@never_cache
def atendimento_hora(request: HttpRequest) -> HttpResponse:
    """Rota antiga da lista de uma hora (#76) — 302 pra `atendimento_lista`.

    A tela virou um dos recortes da lista genérica (#87), que já entende `?h=`.
    Um alias que renderizasse aqui duplicaria template e contexto; o redirect
    canoniza a URL e mantém funcionando o que já foi compartilhado/bookmarkado.
    O `foco` default da rota antiga era "suporte" (o da nova é "todos"), então
    ele é explicitado no redirect pra a hora continuar idêntica.
    """
    params = request.GET.copy()
    if params.get("foco") not in _LISTA_FOCOS:
        params["foco"] = "suporte"
    return HttpResponseRedirect(
        f"{reverse('dashboards:atendimento_lista')}?{params.urlencode()}"
    )


# ---------------------------------------------------------------------------
# Eventos de rede (#78) — registro manual sobre o gráfico horário
# ---------------------------------------------------------------------------
# Formato do <input type="datetime-local">.
_EVENTO_DT_FMT = "%Y-%m-%dT%H:%M"


def _parse_evento_dt(raw: str) -> datetime | None:
    """`datetime-local` (ISO sem fuso) → datetime aware em `_PERIOD_TZ`."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=_PERIOD_TZ)
    return parsed


def _evento_redirect_url(request: HttpRequest, *, erro: bool = False) -> str:
    """URL de volta pras Tendências com os mesmos filtros (veio no POST)."""
    query = request.POST.get("q", "").strip().lstrip("?").replace("#", "")
    if erro:
        query = f"{query}&evento_erro=1" if query else "evento_erro=1"
    url = reverse("dashboards:atendimento_tendencias")
    return f"{url}?{query}" if query else url


def _evento_org(request: HttpRequest) -> Any:
    """Org do usuário se ele pode mexer em evento; None se não pode.

    Mesma regra de permissão da UI de grupos de acesso (`access_management`):
    membership ativa **e** OWNER da org.
    """
    membership = request.user.get_active_membership()
    if membership is None or not membership.is_owner:
        return None
    return membership.organization


@login_required
@never_cache
@require_POST
def evento_rede_novo(request: HttpRequest) -> HttpResponse:
    """Cria um evento de rede a partir do form da página de Tendências (#78)."""
    from apps.atendimento.infrastructure.models import EventoRede

    org = _evento_org(request)
    if org is None:
        return HttpResponseForbidden("Sem permissão para registrar eventos de rede.")

    titulo = request.POST.get("titulo", "").strip()
    tipo = request.POST.get("tipo", "")
    if tipo not in EventoRede.Tipo.values:
        tipo = EventoRede.Tipo.OUTRO.value
    started_at = _parse_evento_dt(request.POST.get("started_at", ""))
    ended_at = _parse_evento_dt(request.POST.get("ended_at", ""))
    if not titulo or started_at is None or (ended_at and ended_at < started_at):
        return HttpResponseRedirect(_evento_redirect_url(request, erro=True))

    EventoRede.objects.create(
        organization=org,
        tipo=tipo,
        titulo=titulo[:255],
        descricao=request.POST.get("descricao", "").strip(),
        started_at=started_at,
        ended_at=ended_at,
        created_by=request.user,
    )
    return HttpResponseRedirect(_evento_redirect_url(request))


@login_required
@never_cache
@require_POST
def evento_rede_editar(request: HttpRequest, evento_id: int) -> HttpResponse:
    """Edita (`action=update`) ou exclui (`action=delete`) um evento de rede.

    Objeto sempre buscado por queryset escopado na org (defesa em profundidade
    além do TenantManager) — evento de outra org responde 404.
    """
    from django.http import Http404

    from apps.atendimento.infrastructure.models import EventoRede

    org = _evento_org(request)
    if org is None:
        return HttpResponseForbidden("Sem permissão para editar eventos de rede.")

    evento = EventoRede.objects.filter(organization=org, pk=evento_id).first()
    if evento is None:
        raise Http404("Evento de rede não encontrado")

    if request.POST.get("action") == "delete":
        evento.delete()
        return HttpResponseRedirect(_evento_redirect_url(request))

    titulo = request.POST.get("titulo", "").strip()
    tipo = request.POST.get("tipo", "")
    started_at = _parse_evento_dt(request.POST.get("started_at", ""))
    ended_at = _parse_evento_dt(request.POST.get("ended_at", ""))
    if not titulo or started_at is None or (ended_at and ended_at < started_at):
        return HttpResponseRedirect(_evento_redirect_url(request, erro=True))

    evento.titulo = titulo[:255]
    if tipo in EventoRede.Tipo.values:
        evento.tipo = tipo
    evento.descricao = request.POST.get("descricao", "").strip()
    evento.started_at = started_at
    evento.ended_at = ended_at
    evento.save(
        update_fields=["titulo", "tipo", "descricao", "started_at", "ended_at",
                       "updated_at"]
    )
    return HttpResponseRedirect(_evento_redirect_url(request))


@login_required
@never_cache
def atendimento_conversao(request: HttpRequest) -> HttpResponse:
    """Desfecho por tag/motivo: churn e conversão após o atendimento (F4)."""
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    try:
        horizon_days = int(request.GET.get("h", 90))
    except (ValueError, TypeError):
        horizon_days = 90
    if horizon_days not in (30, 90, 180):
        horizon_days = 90

    departamento_id: int | None = None
    raw_dep = request.GET.get("departamento", "")
    if raw_dep.isdigit():
        departamento_id = int(raw_dep)

    data = compute_atendimento_conversao(
        org, months=months, horizon_days=horizon_days, departamento_id=departamento_id
    )

    tabelas = [
        {"titulo": "tag", "rows": data["by_tag"][:30]},
        {"titulo": "motivo", "rows": data["by_motivo"][:30]},
    ]

    return render(
        request,
        "dashboards/atendimento_conversao.html",
        {
            **data,
            "tabelas": tabelas,
            "churn_tags_json": charts.atendimento_conversao_bars(
                data["top_churn_tags"], field="churn_pct", color="#ef4444"
            ),
            "conv_tags_json": charts.atendimento_conversao_bars(
                data["top_conv_tags"], field="conv_pct", color="#10b981"
            ),
        },
    )


@login_required
@never_cache
def conversas_ruins(request: HttpRequest) -> HttpResponse:
    """Conversas ruins priorizadas por receita em risco (MRR × score) — issue #49."""
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    # Período em dias (#97): a página é de atendimento, então segue os mesmos
    # presets de Tendências (Hoje, Ontem, …) e o personalizado — não os meses
    # fechados das páginas de série mensal.
    period = _get_period(request)

    departamento_id: int | None = None
    raw_dep = request.GET.get("departamento", "")
    if raw_dep.isdigit():
        departamento_id = int(raw_dep)

    # Recorte da página que o form de período personalizado precisa preservar.
    set_period_extra_params(request, {"departamento": departamento_id})

    data = compute_bad_conversations(
        org,
        start=period.start,
        end=period.end,
        departamento_id=departamento_id,
    )
    for r in data["rows"]:
        r["mrr_str"] = _fmt_brl(r["mrr"])
        r["priority_str"] = _fmt_brl(r["priority"])

    return render(
        request,
        "dashboards/conversas_ruins.html",
        {
            **data,
            "total_mrr_at_stake_str": _fmt_brl(data["total_mrr_at_stake"]),
        },
    )


@login_required
@never_cache
def atendimento_detail(request: HttpRequest, atendimento_id: int) -> HttpResponse:
    """Drill-down de uma conversa: contexto de receita/risco + timeline de mensagens."""
    from django.http import Http404

    from apps.atendimento.application.messages import get_or_fetch_messages
    from apps.atendimento.infrastructure.models import Atendimento

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    at = (
        Atendimento.objects.filter(organization=org, pk=atendimento_id)
        .select_related("departamento", "customer")
        .first()
    )
    if at is None:
        raise Http404("Atendimento não encontrado")

    detail = compute_atendimento_detail(org, at)
    messages = get_or_fetch_messages(org, at)
    qa_review = at.qa_reviews.filter(organization=org).first()

    return render(
        request,
        "dashboards/atendimento_detail.html",
        {
            "at": at,
            "status_label": at.get_status_display(),
            "messages": messages,
            "qa": qa_review,
            "mrr_str": _fmt_brl(detail["mrr"]),
            "expected_loss_str": _fmt_brl(detail["expected_loss"]),
            "risk_label": detail["risk_label"],
            "risk_level": detail["risk_level"],
            "risk_fraction_pct": detail["risk_fraction_pct"],
            "churn_signals": detail["churn_signals"],
            "tma_str": detail["tma_str"],
        },
    )


@login_required
@never_cache
def qa_supervisor(request: HttpRequest) -> HttpResponse:
    """Scorecard da IA supervisora de QA (LLM-as-judge) — issue #51."""
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    departamento_id: int | None = None
    raw_dep = request.GET.get("departamento", "")
    if raw_dep.isdigit():
        departamento_id = int(raw_dep)

    cohort = request.GET.get("cohort", "human")
    if cohort not in ("human", "bot", "all"):
        cohort = "human"

    data = compute_qa_overview(
        org, months=months, departamento_id=departamento_id, cohort=cohort
    )
    return render(request, "dashboards/qa_supervisor.html", data)


@login_required
@never_cache
def tecnicos(request: HttpRequest) -> HttpResponse:
    """Qualidade e produção de técnicos — ranking + retorno + perfil + evolução."""
    from apps.helpdesk.application.os_classification import (
        category_label,
        classify_subject,
    )
    from apps.helpdesk.application.os_lookups import load_os_lookups
    from apps.helpdesk.application.technician_stats import (
        PROFILE_FIELD,
        PROFILE_INTERNAL,
        compute_technician_monthly,
        compute_technician_stats,
    )
    from apps.helpdesk.infrastructure.models import Ticket

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect
    months = _get_months(request)

    # Filtro de perfil (rua/interno) — combinável com o recorte temporal.
    profile_f = request.GET.get("profile", "").strip().upper()
    if profile_f not in (PROFILE_FIELD, PROFILE_INTERNAL):
        profile_f = ""

    lookups = load_os_lookups(org)
    subject_to_category = {
        sid: classify_subject(name) for sid, name in lookups.subject_map.items()
    }
    now = timezone.now()
    window_start = (now - relativedelta(months=months)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    tickets = list(
        Ticket.objects.filter(organization=org, opened_at__gte=window_start).values(
            "technician_id", "customer_external_id", "subject_id",
            "status", "opened_at", "closed_at",
        )
    )
    stats = compute_technician_stats(
        tickets, subject_to_category=subject_to_category
    )

    # Resolve nomes (técnico + tipo predominante) e formata tempo médio.
    all_rows = []
    for s in stats:
        avg_hours = s["avg_res_hours"]
        if avg_hours >= 24:
            avg_str = f"{avg_hours / 24:.1f} dias"
        elif avg_hours > 0:
            avg_str = f"{avg_hours:.1f}h"
        else:
            avg_str = "—"
        all_rows.append({
            **s,
            "technician": lookups.technician_name(s["technician_id"]),
            "top_subject": lookups.subject_name(s["top_subject_id"]),
            "avg_res_str": avg_str,
        })

    # Contagem por perfil antes de aplicar o filtro (pros KPIs de perfil).
    field_count = sum(1 for r in all_rows if r.get("profile") == PROFILE_FIELD)
    internal_count = sum(1 for r in all_rows if r.get("profile") == PROFILE_INTERNAL)

    rows = (
        [r for r in all_rows if r.get("profile") == profile_f]
        if profile_f
        else all_rows
    )

    # --- KPIs (agregados sobre o conjunto filtrado) ---
    active_techs = len(rows)
    total_os = sum(r["total"] for r in rows)
    total_closed = sum(r["closed"] for r in rows)
    total_returns = sum(r["returns"] for r in rows)
    avg_solution = round(total_closed / total_os * 100, 1) if total_os else 0.0
    return_rate = round(total_returns / total_os * 100, 1) if total_os else 0.0

    # Tempo médio global (ponderado pelas OS fechadas com tempo medido).
    weighted_hours = sum(r["avg_res_hours"] * r["closed"] for r in rows)
    avg_res_hours = weighted_hours / total_closed if total_closed else 0.0
    if avg_res_hours >= 24:
        avg_res_str = f"{avg_res_hours / 24:.1f} dias"
    else:
        avg_res_str = f"{avg_res_hours:.1f}h"

    top_rows = sorted(rows, key=lambda r: r["total"], reverse=True)[:12]

    # --- Evolução temporal: produção mês a mês dos top técnicos (filtrados) ---
    visible_ids = {r["technician_id"] for r in rows}
    monthly = compute_technician_monthly(
        [t for t in tickets if t["technician_id"] in visible_ids],
        now=now,
        months=months,
    )
    monthly_top = [
        {**series, "technician": lookups.technician_name(series["technician_id"])}
        for series in monthly["per_tech"][:6]
    ]
    monthly_data = {"labels": monthly["labels"], "per_tech": monthly_top}

    # --- Recorte por tipo de atendimento: mix de categorias dos top técnicos ---
    cat_keys: list[str] = []
    for r in top_rows:
        for cat in (r.get("category_counts") or {}):
            if cat not in cat_keys:
                cat_keys.append(cat)
    category_meta = [{"key": k, "label": category_label(k)} for k in cat_keys]
    category_data = {
        "categories": category_meta,
        "rows": [
            {"technician": r["technician"], "counts": r.get("category_counts") or {}}
            for r in top_rows
        ],
    }

    return render(
        request,
        "dashboards/tecnicos.html",
        {
            "active_techs": active_techs,
            "avg_solution_str": f"{avg_solution:.1f}%",
            "avg_solution": avg_solution,
            "avg_res_str": avg_res_str,
            "return_rate_str": f"{return_rate:.1f}%",
            "return_rate": return_rate,
            "rows": rows,
            "synced": bool(lookups.technician_map),
            "profile_filter": profile_f,
            "field_count": field_count,
            "internal_count": internal_count,
            "production_chart_json": charts.technician_production_bar(top_rows),
            "solution_chart_json": charts.technician_solution_bar(top_rows),
            "monthly_chart_json": charts.technician_monthly_lines(monthly_data),
            "category_chart_json": charts.technician_category_stacked(category_data),
        },
    )


@login_required
@never_cache
def network(request: HttpRequest) -> HttpResponse:
    from apps.network.infrastructure.models import Connection

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    qs = Connection.objects.filter(organization=org)
    total = qs.count()

    # KPIs por status
    status_counts = {
        row["status"]: row["count"]
        for row in qs.values("status").annotate(count=Count("id"))
    }
    online_count = status_counts.get("ONLINE", 0)
    offline_count = status_counts.get("OFFLINE", 0)
    blocked_count = status_counts.get("BLOCKED", 0)

    # Uptime % = online / (online + offline) — ignora bloqueados (não são falha de rede)
    active_base = online_count + offline_count
    uptime_pct = round(online_count / active_base * 100, 1) if active_base else 0.0

    # Distribuição por status (donut)
    status_labels = {
        "ONLINE": "Online",
        "OFFLINE": "Offline",
        "BLOCKED": "Bloqueado",
        "UNKNOWN": "Desconhecido",
    }
    status_dist = [
        {
            "status": status_labels.get(s, s),
            "status_key": s,
            "count": c,
        }
        for s, c in sorted(status_counts.items(), key=lambda kv: -kv[1])
    ]

    # Conexões por concentrador (NAS) — top 10
    nas_dist = [
        {"nas_ip": row["nas_ip"] or "—", "count": row["count"]}
        for row in (
            qs.exclude(nas_ip="")
            .values("nas_ip")
            .annotate(count=Count("id"))
            .order_by("-count")[:10]
        )
    ]

    # Top 20 consumidores de banda (rx + tx)
    top_consumers = list(
        qs.select_related("customer")
        .annotate(total_bytes=F("rx_bytes") + F("tx_bytes"))
        .order_by("-total_bytes")[:20]
        .values(
            "login", "customer__name", "customer_external_id",
            "status", "nas_ip", "rx_bytes", "tx_bytes", "total_bytes",
        )
    )
    for c in top_consumers:
        c["status_label"] = status_labels.get(c["status"], c["status"])
        c["customer_name"] = c["customer__name"] or f"Cliente #{c['customer_external_id']}"
        c["total_gb"] = round((c["total_bytes"] or 0) / 1024**3, 2)

    # Consumo de banda agregado (accounting RADIUS / radusuarios_consumo)
    bandwidth = compute_bandwidth_summary(org)

    # Histórico temporal — série de snapshots de rede (#35)
    history = compute_network_history(org, days=30)

    # Clientes pagantes (contrato ativo) sem conexão online — receita em risco
    offline_active = compute_offline_active_customers(org)

    # CTOs — Caixas de Distribuição FTTH (ocupação de portas por projeto)
    cto = compute_cto_summary(org)
    cto_history = compute_cto_history(org, months=12)

    return render(
        request,
        "dashboards/network.html",
        {
            "total": total,
            "offline_active": offline_active,
            "offline_active_mrr_str": (
                f"{offline_active['mrr_at_risk']:,.2f}".replace(",", "X")
                .replace(".", ",")
                .replace("X", ".")
            ),
            "online_count": online_count,
            "offline_count": offline_count,
            "blocked_count": blocked_count,
            "uptime_pct": uptime_pct,
            "uptime_pct_str": f"{uptime_pct:.1f}%",
            "status_dist": status_dist,
            "top_consumers": top_consumers,
            "status_chart_json": charts.connection_status_pie(status_dist),
            "nas_chart_json": charts.connections_by_nas_bar(nas_dist),
            "history_has_data": history["count"] > 0,
            "history_chart_json": charts.network_history_lines(history),
            "bandwidth": bandwidth,
            "bandwidth_has_data": bandwidth["total_bytes"] > 0,
            "bandwidth_total_gb_str": f"{bandwidth['total_gb']:,.2f}".replace(",", "."),
            "bandwidth_avg_gb_str": f"{bandwidth['avg_per_customer_gb']:,.2f}".replace(",", "."),
            "bandwidth_avg_subtitle": f"{bandwidth['customer_count']} clientes com consumo",
            "bandwidth_chart_json": charts.bandwidth_top_consumers_bar(
                bandwidth["top_consumers"]
            ),
            # CTOs
            "cto": cto,
            "cto_has_data": cto["total_ctos"] > 0,
            "cto_total_str": str(cto["total_ctos"]),
            "cto_occupied_str": f"{cto['total_occupied']:,}".replace(",", "."),
            "cto_free_str": f"{cto['total_free']:,}".replace(",", "."),
            "cto_occupancy_str": f"{cto['occupancy_pct']:.1f}%",
            "cto_chart_json": charts.cto_by_project_stacked_bar(cto["by_project"]),
            "cto_history": cto_history,
            "cto_history_has_data": cto_history["count"] > 0,
            "cto_history_chart_json": charts.cto_history_chart(cto_history) if cto_history["count"] > 0 else "",
        },
    )


@login_required
@never_cache
def sales(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    months = _get_months(request)
    funnel = compute_sales_funnel(org)
    origin = compute_lead_origin(org)
    net_adds = compute_net_adds_series(org, months=months)
    pipeline = compute_pipeline_aging(org)

    net_adds_total = sum(p["net"] for p in net_adds)

    return render(
        request,
        "dashboards/sales.html",
        {
            "funnel": funnel,
            "leads_new_month_str": f"{funnel['leads_new_month']:,}".replace(",", "."),
            "conversion_str": f"{funnel['lead_to_won_pct']:.1f}%",
            "conversion_subtitle": (
                f"{funnel['won_count']} ganhos de {funnel['total_leads']} leads"
            ),
            "pipeline_value_str": _fmt_brl(funnel["pipeline_value"]),
            "pipeline_subtitle": f"{funnel['open_count']} negociações em andamento",
            "net_adds_total": net_adds_total,
            "net_adds_total_str": f"{net_adds_total:+,}".replace(",", "."),
            "pipeline_list": pipeline,
            "funnel_chart_json": charts.sales_funnel_chart(funnel["funnel_stages"]),
            "net_adds_chart_json": charts.net_adds_bar_chart(net_adds),
            "lead_origin_chart_json": charts.lead_origin_pie(origin),
        },
    )


@login_required
@never_cache
def customers(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    query = request.GET.get("q", "").strip()

    # --- Filtros de segmentação (combináveis com a busca) ---
    _STATUS_OPTS = {"ACTIVE", "BLOCKED", "CANCELED"}
    _RISK_OPTS = {"HIGH", "MEDIUM", "LOW", "NONE"}
    status_f = request.GET.get("status", "").strip().upper()
    status_f = status_f if status_f in _STATUS_OPTS else ""
    risk_f = request.GET.get("risk", "").strip().upper()
    risk_f = risk_f if risk_f in _RISK_OPTS else ""

    def _parse_float(name: str) -> float | None:
        raw = request.GET.get(name, "").strip().replace(",", ".")
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    mrr_min = _parse_float("mrr_min")
    mrr_max = _parse_float("mrr_max")
    overdue_f = request.GET.get("overdue") == "1"
    equip_f = request.GET.get("equip") == "1"

    def _parse_int(name: str) -> int | None:
        raw = request.GET.get(name, "").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    ticket_days = _parse_int("ticket_days")

    has_filters = any(
        [status_f, risk_f, mrr_min is not None, mrr_max is not None,
         overdue_f, equip_f, ticket_days is not None]
    )

    results = search_customers(
        org,
        query=query,
        limit=100,
        status=status_f or None,
        risk_level=risk_f or None,
        mrr_min=mrr_min,
        mrr_max=mrr_max,
        overdue=overdue_f,
        has_equipment=equip_f,
        recent_ticket_days=ticket_days,
    )

    # Painel "clientes a focar" só na visão padrão — em busca/filtro, só resultados.
    show_priority = not query and not has_filters
    priority = compute_priority_customers(org, limit=15) if show_priority else None

    return render(
        request,
        "dashboards/customers_list.html",
        {
            "query": query,
            "results": results,
            "result_count": len(results),
            "priority": priority,
            "revenue_in_focus_str": (
                _fmt_brl(priority["revenue_in_focus"]) if priority else ""
            ),
            "filters": {
                "status": status_f,
                "risk": risk_f,
                "mrr_min": request.GET.get("mrr_min", "").strip(),
                "mrr_max": request.GET.get("mrr_max", "").strip(),
                "overdue": overdue_f,
                "equip": equip_f,
                "ticket_days": ticket_days,
            },
            "has_filters": has_filters,
        },
    )


@login_required
@never_cache
def customer_detail(request: HttpRequest, customer_id: int) -> HttpResponse:
    from django.http import Http404

    from apps.customers.infrastructure.models import Customer

    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    customer = (
        Customer.objects.filter(organization=org, pk=customer_id).first()
    )
    if customer is None:
        raise Http404("Cliente não encontrado")

    data = compute_customer_360(org, customer)
    fin = data["financial"]

    return render(
        request,
        "dashboards/customer_detail.html",
        {
            "c": data["customer"],
            "churn": data["churn"],
            "contracts": data["contracts"],
            "contracts_count": data["contracts_count"],
            "mrr_active_str": _fmt_brl(data["mrr_active"]),
            "financial": fin,
            "overdue_str": _fmt_brl(fin["overdue_amount"]),
            "open_str": _fmt_brl(fin["open_amount"]),
            "paid_total_str": _fmt_brl(fin["paid_total"]),
            "support": data["support"],
            "network": data["network"],
            "network_total_gb_str": f"{data['network']['total_gb']:,.2f}".replace(",", "."),
            "equipment": data["equipment"],
            "timeline": data["timeline"],
        },
    )


@login_required
@never_cache
def risk(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    summary = compute_churn_risk_summary(org)
    top = compute_top_risk_customers(org, limit=20)

    return render(
        request,
        "dashboards/risk.html",
        {
            "summary": summary,
            "top": top,
            "revenue_at_risk_str": _fmt_brl(summary["revenue_at_risk"]),
            "risk_level_json": charts.churn_risk_level_pie(summary),
            "risk_signal_json": charts.churn_risk_signal_bar(summary["signal_distribution"]),
        },
    )


@login_required
@never_cache
def settings_view(request: HttpRequest) -> HttpResponse:
    """Preferências do usuário + (owner) convite de usuários e gestão de acesso."""
    from apps.tenancy.invites import invite_user
    from apps.tenancy.models import (
        AccessGroup,
        OrganizationInvite,
        OrganizationMembership,
    )

    user = request.user
    membership = user.get_active_membership()
    is_owner = bool(membership and membership.is_owner)
    org = membership.organization if membership else None

    saved = False
    invite_msg = None
    if request.method == "POST":
        action = request.POST.get("action", "prefs")
        if action == "invite" and is_owner and org is not None:
            email = request.POST.get("email", "").strip()
            role = request.POST.get("role", OrganizationMembership.Role.MEMBER)
            group_id = request.POST.get("access_group", "")
            group = None
            if group_id.isdigit():
                group = AccessGroup.objects.filter(
                    organization=org, id=int(group_id)
                ).first()
            if email:
                result = invite_user(
                    organization=org, email=email, role=role,
                    access_group=group, invited_by=user, request=request,
                )
                sent = "e-mail de acesso enviado" if result.email_sent else (
                    "conta provisionada (não consegui enviar o e-mail — envie o link "
                    "de definir senha manualmente)"
                )
                invite_msg = f"{email}: {sent}."
            else:
                invite_msg = "Informe um e-mail."
        else:
            user.churn_digest_weekly = bool(request.POST.get("churn_digest_weekly"))
            user.churn_digest_monthly = bool(request.POST.get("churn_digest_monthly"))
            user.save(update_fields=["churn_digest_weekly", "churn_digest_monthly"])
            saved = True

    ctx: dict[str, Any] = {
        "churn_digest_weekly": user.churn_digest_weekly,
        "churn_digest_monthly": user.churn_digest_monthly,
        "saved": saved,
        "is_owner": is_owner,
        "invite_msg": invite_msg,
    }
    if is_owner and org is not None:
        ctx["access_groups"] = list(
            AccessGroup.objects.filter(organization=org).order_by("name")
        )
        ctx["members"] = list(
            OrganizationMembership.objects.filter(organization=org)
            .select_related("user", "access_group")
            .order_by("user__email")
        )
        ctx["roles"] = OrganizationMembership.Role.choices
        ctx["recent_invites"] = list(
            OrganizationInvite.objects.filter(organization=org)
            .select_related("access_group")
            .order_by("-created_at")[:10]
        )

    return render(request, "dashboards/settings.html", ctx)


@login_required
@never_cache
def access_management(request: HttpRequest) -> HttpResponse:
    """Gestão self-service de grupos de acesso (owner-only, #70)."""
    from apps.dashboards.pages import sections
    from apps.tenancy.models import AccessGroup, OrganizationMembership

    user = request.user
    membership = user.get_active_membership()
    if membership is None or not membership.is_owner:
        return HttpResponseRedirect(reverse("dashboards:no_access"))
    org = membership.organization

    valid_keys = {p["key"] for grp in sections() for p in grp["pages"]}
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "create_group":
            name = request.POST.get("name", "").strip()
            pages = [k for k in request.POST.getlist("pages") if k in valid_keys]
            if name:
                AccessGroup.objects.update_or_create(
                    organization=org, name=name, defaults={"allowed_pages": pages}
                )
        elif action == "update_group":
            gid = request.POST.get("group_id", "")
            if gid.isdigit():
                g = AccessGroup.objects.filter(organization=org, id=int(gid)).first()
                if g:
                    g.allowed_pages = [
                        k for k in request.POST.getlist("pages") if k in valid_keys
                    ]
                    new_name = request.POST.get("name", "").strip()
                    if new_name:
                        g.name = new_name
                    g.save()
        elif action == "delete_group":
            gid = request.POST.get("group_id", "")
            if gid.isdigit():
                AccessGroup.objects.filter(organization=org, id=int(gid)).delete()
        elif action == "assign":
            mid = request.POST.get("membership_id", "")
            gid = request.POST.get("group_id", "")
            m = (
                OrganizationMembership.objects.filter(organization=org, id=int(mid))
                .first()
                if mid.isdigit()
                else None
            )
            if m and not m.is_owner:
                m.access_group = (
                    AccessGroup.objects.filter(organization=org, id=int(gid)).first()
                    if gid.isdigit()
                    else None
                )
                m.save(update_fields=["access_group"])
        return HttpResponseRedirect(
            f"{reverse('dashboards:access_management')}?ok=1"
        )

    groups = list(AccessGroup.objects.filter(organization=org).order_by("name"))
    for g in groups:
        g.pages_set = set(g.allowed_pages or [])
    members = list(
        OrganizationMembership.objects.filter(organization=org)
        .select_related("user", "access_group")
        .order_by("user__email")
    )
    return render(
        request,
        "dashboards/access_management.html",
        {
            "sections": sections(),
            "groups": groups,
            "members": members,
            "saved": request.GET.get("ok") == "1",
        },
    )
