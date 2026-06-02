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
    compute_blocked_at_risk_summary,
    compute_blocked_duration_distribution,
    compute_burn_rate,
    compute_cash_received_series,
    compute_cashflow_series,
    compute_churn_by_plan,
    compute_churn_by_reason,
    compute_churn_plan_detail,
    compute_churn_summary,
    compute_contract_status_trend,
    compute_delinquency_trend,
    compute_dre,
    compute_dre_by_account,
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
    compute_recovery_rate,
    compute_revenue_forecast,
    compute_top_delinquent_invoices,
)
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
    churn_subtitle = f"{kpis['canceled_this_month']} cancelados · {kpis['new_this_month']} novos neste mês"
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
            "last_sync": last_sync,
            "mrr_chart_json": charts.mrr_line_chart(mrr_series),
            "aging_chart_json": charts.aging_bar_chart(aging),
            "delinquency_trend_json": charts.delinquency_trend_chart(delinquency_trend),
            "contract_status_json": charts.contract_status_stacked_chart(contract_status_trend),
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

    # Taxa de recebimento — extraída do primeiro mês do forecast (é a mesma para todos)
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
            "cur_despesas_str": _fmt_brl(cur["despesas"]),
            "cur_ebitda_str": _fmt_brl(cur["ebitda"]),
            "cur_margin_str": f"{cur['ebitda_margin_pct']:.1f}%",
            "dre_chart_json": charts.dre_grouped_bar(
                dre_data["mrr_series"], dre_data["expense_series"]
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
            "volume_chart_json": charts.ticket_volume_trend(volume_series),
            "priority_chart_json": charts.ticket_priority_pie(priority_dist),
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

    return render(
        request,
        "dashboards/network.html",
        {
            "total": total,
            "online_count": online_count,
            "offline_count": offline_count,
            "blocked_count": blocked_count,
            "uptime_pct": uptime_pct,
            "uptime_pct_str": f"{uptime_pct:.1f}%",
            "status_dist": status_dist,
            "top_consumers": top_consumers,
            "status_chart_json": charts.connection_status_pie(status_dist),
            "nas_chart_json": charts.connections_by_nas_bar(nas_dist),
        },
    )
