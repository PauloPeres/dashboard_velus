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
    compute_cash_received_series,
    compute_kpis,
    compute_mrr_series,
    compute_pipeline_by_status,
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
