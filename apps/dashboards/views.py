"""Views dos dashboards.

Todas exigem login + membership ativa. Tenant é resolvido pelo middleware
e via context_processor exposto em `current_organization`.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
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
    compute_expense_by_category,
    compute_expense_by_supplier,
    compute_expense_series,
    compute_kpis,
    compute_ltv_distribution,
    compute_mrr_churn_series,
    compute_mrr_series,
    compute_pipeline_by_status,
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


@login_required
@never_cache
def executive(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    kpis = compute_kpis(org)
    mrr_series = compute_mrr_series(org, months=12)
    aging = compute_aging_distribution(org)
    delinquency_trend = compute_delinquency_trend(org, months=12)
    contract_status_trend = compute_contract_status_trend(org, months=12)

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

    kpis = compute_kpis(org)
    mrr_series = compute_mrr_series(org, months=12)
    arpu_data = compute_arpu_by_plan(org)
    status_trend = compute_contract_status_trend(org, months=12)
    churn_plan = compute_churn_by_plan(org, months=3)

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

    cashflow_data = compute_cashflow_series(org, months=12)
    supplier_data = compute_expense_by_supplier(org, months=3)
    category_data = compute_expense_by_category(org, months=3)

    return render(
        request,
        "dashboards/cashflow.html",
        {
            "cashflow_data": cashflow_data,
            "supplier_data": supplier_data,
            "category_data": category_data,
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

    historical = compute_mrr_series(org, months=12)
    forecast_data = compute_revenue_forecast(org, months_ahead=12)
    dre_data = compute_dre(org, months=12)

    cur = dre_data["current_month"]
    ytd = dre_data["ytd"]

    return render(
        request,
        "dashboards/forecast.html",
        {
            "historical": historical,
            "forecast_data": forecast_data,
            "dre_summary": cur,
            "ytd": ytd,
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

    dre_data = compute_dre(org, months=12)

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

    burn_data = compute_burn_rate(org, months=6)
    expense_series = compute_expense_series(org, months=12)

    return render(
        request,
        "dashboards/burn.html",
        {
            "burn": burn_data,
            "expense_series": expense_series,
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

    kpis = compute_kpis(org)
    aging = compute_aging_distribution(org)
    top_delinquent = compute_top_delinquent_invoices(org, limit=50)
    cash_series = compute_cash_received_series(org, months=12)
    delinquency_trend = compute_delinquency_trend(org, months=12)
    status_trend = compute_contract_status_trend(org, months=12)

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
            # charts
            "aging_chart_json": charts.aging_bar_chart(aging),
            "delinquency_trend_json": charts.delinquency_trend_chart(delinquency_trend),
            "cash_chart_json": charts.cash_received_chart(cash_series),
            "blocked_series_json": charts.blocked_trend_line(blocked_series),
        },
    )


@login_required
@never_cache
def contracts(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    kpis = compute_kpis(org)
    status_trend = compute_contract_status_trend(org, months=12)
    arpu_data = compute_arpu_by_plan(org)
    churn_plan = compute_churn_by_plan(org, months=3)
    blocked_dist = compute_blocked_duration_distribution(org)
    at_risk_summary = compute_blocked_at_risk_summary(org, min_days=30)
    at_risk_list = compute_at_risk_contracts(org, min_days=30, limit=50)

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
            # charts
            "status_trend_json": charts.contract_status_stacked_chart(status_trend),
            "arpu_chart_json": charts.arpu_bar_chart(arpu_data),
            "churn_plan_json": charts.churn_by_plan_bar(churn_plan),
            "blocked_dist_json": charts.blocked_duration_histogram(blocked_dist),
        },
    )


@login_required
@never_cache
def churn(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    summary = compute_churn_summary(org)
    mrr_series = compute_mrr_churn_series(org, months=12)
    reasons = compute_churn_by_reason(org, months=12)
    ltv_dist = compute_ltv_distribution(org)
    plan_detail = compute_churn_plan_detail(org, months=12)

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
