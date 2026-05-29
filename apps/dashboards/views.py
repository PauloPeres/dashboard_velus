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
    compute_burn_rate,
    compute_cash_received_series,
    compute_cashflow_series,
    compute_dre,
    compute_expense_by_category,
    compute_expense_by_supplier,
    compute_expense_series,
    compute_kpis,
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

    return render(
        request,
        "dashboards/executive.html",
        {
            "kpis": kpis,
            "mrr_chart_json": charts.mrr_line_chart(mrr_series),
            "aging_chart_json": charts.aging_bar_chart(aging),
        },
    )


@login_required
@never_cache
def revenue(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org(request)
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    arpu_data = compute_arpu_by_plan(org)
    pipeline = compute_pipeline_by_status(org)
    mrr_series = compute_mrr_series(org, months=12)

    return render(
        request,
        "dashboards/revenue.html",
        {
            "arpu_data": arpu_data,
            "pipeline": pipeline,
            "mrr_chart_json": charts.mrr_line_chart(mrr_series),
            "arpu_chart_json": charts.arpu_bar_chart(arpu_data),
            "pipeline_chart_json": charts.pipeline_pie(pipeline),
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

    return render(
        request,
        "dashboards/forecast.html",
        {
            "historical": historical,
            "forecast_data": forecast_data,
            "dre_summary": dre_data["current_month"],
            "ytd": dre_data["ytd"],
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

    return render(
        request,
        "dashboards/dre.html",
        {
            "dre": dre_data,
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

    aging = compute_aging_distribution(org)
    top_delinquent = compute_top_delinquent_invoices(org, limit=50)
    cash_series = compute_cash_received_series(org, months=12)

    return render(
        request,
        "dashboards/financial.html",
        {
            "aging": aging,
            "top_delinquent": top_delinquent,
            "aging_chart_json": charts.aging_bar_chart(aging),
            "cash_chart_json": charts.cash_received_chart(cash_series),
        },
    )
