"""Views dos dashboards.

Todas exigem login + membership ativa. Tenant é resolvido pelo middleware
e via context_processor exposto em `current_organization`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.analytics.application.aggregations import (
    compute_aging_distribution,
    compute_arpu_by_plan,
    compute_at_risk_contracts,
    compute_bandwidth_summary,
    compute_blocked_at_risk_summary,
    compute_blocked_duration_distribution,
    compute_burn_rate,
    compute_cash_received_series,
    compute_cashflow_series,
    compute_churn_by_plan,
    compute_churn_by_reason,
    compute_churn_plan_detail,
    compute_churn_risk_summary,
    compute_churn_summary,
    compute_contract_kpi_trend,
    compute_contract_status_trend,
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
    compute_ltv_distribution,
    compute_mrr_churn_series,
    compute_mrr_series,
    compute_mao_de_obra_detail,
    compute_people_expenses,
    compute_pipeline_by_status,
    compute_lead_origin,
    compute_net_adds_series,
    compute_offline_active_customers,
    compute_pipeline_aging,
    compute_priority_customers,
    compute_recovery_rate,
    compute_revenue_comparison,
    compute_revenue_forecast,
    compute_sales_funnel,
    compute_support_sla,
    compute_top_delinquent_invoices,
    compute_top_risk_customers,
    search_customers,
)
from apps.analytics.application.network_snapshots import compute_network_history
from apps.shared.context import get_current_organization

from . import charts


def _require_org(request: HttpRequest) -> Any:  # noqa: ARG001 — assinatura uniforme para uso futuro
    org = get_current_organization()
    if org is None:
        return HttpResponseRedirect("/admin/")  # sem org → fallback admin
    return org


@login_required
@never_cache
def home(request: HttpRequest) -> HttpResponse:  # noqa: ARG001
    return HttpResponseRedirect(reverse("dashboards:executive"))


def _fmt_brl(value: float) -> str:
    """Formata float como 'R$ 326.802' (separador de milhar BR, sem centavos)."""
    v = int(round(value))
    formatted = f"{v:,}".replace(",", ".")
    return f"R$ {formatted}"


def _get_months(request: HttpRequest) -> int:
    """Lê seletor de período ?months=N. Válidos: 1, 2, 3, 6, 12, 24. Default: 12."""
    try:
        v = int(request.GET.get("months", 12))
        return v if v in (1, 2, 3, 6, 12, 24) else 12
    except (ValueError, TypeError):
        return 12


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
    current_month_key = timezone.now().strftime("%Y-%m")
    cash_realized_recent = [c for c in cash_series if c["month"] <= current_month_key][-6:]
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
    # dre_detalhe tem seu próprio seletor from/to; o global ?months é fallback
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
    months = _get_months(request)

    # All tickets for this org (TenantManager filters by org)
    qs = Ticket.objects.filter(organization=org)
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # KPIs
    open_count = qs.exclude(status="CLOSED").count()

    closed_this_month_qs = qs.filter(status="CLOSED", closed_at__gte=month_start)
    closed_this_month = closed_this_month_qs.count()

    # Avg resolution time (closed tickets this month)
    avg_resolution = closed_this_month_qs.filter(
        opened_at__isnull=False,
        closed_at__isnull=False,
    ).aggregate(
        avg_hours=Avg(F("closed_at") - F("opened_at"))
    )["avg_hours"]
    avg_resolution_hours = 0.0
    if avg_resolution is not None:
        avg_resolution_hours = avg_resolution.total_seconds() / 3600

    # SLA % (closed within 24h / total closed this month)
    sla_threshold = timedelta(hours=24)
    if closed_this_month > 0:
        from django.db.models import DurationField, ExpressionWrapper
        within_sla = (
            closed_this_month_qs
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
        sla_pct = round(within_sla / closed_this_month * 100, 1)
    else:
        sla_pct = 0.0

    # Volume trend (opened vs closed per month, last N months)
    volume_series = []
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
        opened_m = qs.filter(opened_at__gte=m_start, opened_at__lt=m_end).count()
        closed_m = qs.filter(closed_at__gte=m_start, closed_at__lt=m_end).count()
        volume_series.append({
            "month": m_start.strftime("%Y-%m"),
            "label": m_start.strftime("%b/%y"),
            "opened": opened_m,
            "closed": closed_m,
        })

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

    # SLA por tipo de atendimento (Manutenção/Instalação/...) — últimos 30 dias
    # com comparativo vs os 30 dias anteriores.
    sla_by_type = compute_support_sla(org, period_days=30)

    return render(
        request,
        "dashboards/operations.html",
        {
            "open_count": open_count,
            "closed_this_month": closed_this_month,
            "avg_resolution_str": avg_res_str,
            "sla_pct": sla_pct,
            "sla_pct_str": f"{sla_pct:.1f}%",
            "open_tickets": open_tickets,
            "priority_dist": priority_dist,
            "sla_by_type": sla_by_type,
            "volume_chart_json": charts.ticket_volume_trend(volume_series),
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
    """Preferências do usuário — opt-in dos digests de risco de churn por email."""
    user = request.user
    saved = False
    if request.method == "POST":
        user.churn_digest_weekly = bool(request.POST.get("churn_digest_weekly"))
        user.churn_digest_monthly = bool(request.POST.get("churn_digest_monthly"))
        user.save(update_fields=["churn_digest_weekly", "churn_digest_monthly"])
        saved = True

    return render(
        request,
        "dashboards/settings.html",
        {
            "churn_digest_weekly": user.churn_digest_weekly,
            "churn_digest_monthly": user.churn_digest_monthly,
            "saved": saved,
        },
    )
