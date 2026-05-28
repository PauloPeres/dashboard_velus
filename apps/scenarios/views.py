"""Views dos simuladores."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from apps.scenarios.domain.services import (
    calculate_clt_cost,
    calculate_pj_vs_clt,
    calculate_simples_split,
    load_assumptions,
)
from apps.scenarios.forms import (
    PjVsCltForm,
    SalaryAdjustForm,
    SimplesSplitForm,
    UnionEspForm,
)
from apps.scenarios.infrastructure.models import Scenario
from apps.shared.context import get_current_organization


def _require_org() -> Any:
    org = get_current_organization()
    if org is None:
        return HttpResponseRedirect("/admin/")
    return org


@login_required
@never_cache
def index(request: HttpRequest) -> HttpResponse:  # noqa: ARG001
    return HttpResponseRedirect(reverse("scenarios:pj_vs_clt"))


@login_required
@never_cache
def pj_vs_clt(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    assumptions = load_assumptions(org)
    if request.method == "POST":
        form = PjVsCltForm(request.POST)
        if form.is_valid():
            result = calculate_pj_vs_clt(
                n_workers=form.cleaned_data["n_workers"],
                pj_monthly_per_worker=form.cleaned_data["pj_monthly_per_worker"],
                clt_salary=form.cleaned_data["clt_salary"],
                assumptions=assumptions,
            )
            if request.POST.get("save_scenario"):
                Scenario.objects.create(
                    organization=org,
                    type=Scenario.Type.PJ_VS_CLT,
                    name=request.POST.get("scenario_name") or "PJ vs CLT",
                    inputs={k: str(v) for k, v in form.cleaned_data.items()},
                    results=result.as_dict(),
                    base_at=timezone.now(),
                )
            return render(
                request, "scenarios/pj_vs_clt.html",
                {"form": form, "result": result.as_dict(), "result_obj": result},
            )
    else:
        form = PjVsCltForm()
    return render(request, "scenarios/pj_vs_clt.html", {"form": form, "result": None})


@login_required
@never_cache
def simples_split(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    assumptions = load_assumptions(org)
    result_dict = None
    if request.method == "POST":
        form = SimplesSplitForm(request.POST)
        if form.is_valid():
            result = calculate_simples_split(
                total_revenue=form.cleaned_data["total_revenue"],
                split_pct=form.cleaned_data["split_pct"],
                assumptions=assumptions,
            )
            result_dict = result.as_dict()
            if request.POST.get("save_scenario"):
                Scenario.objects.create(
                    organization=org,
                    type=Scenario.Type.SIMPLES_SPLIT,
                    name=request.POST.get("scenario_name") or "Split CNPJ",
                    inputs={k: str(v) for k, v in form.cleaned_data.items()},
                    results=result_dict,
                    base_at=timezone.now(),
                )
    else:
        form = SimplesSplitForm()
    return render(
        request, "scenarios/simples_split.html",
        {"form": form, "result": result_dict},
    )


@login_required
@never_cache
def salary_adjust(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    assumptions = load_assumptions(org)
    result_dict = None
    if request.method == "POST":
        form = SalaryAdjustForm(request.POST)
        if form.is_valid():
            current = calculate_clt_cost(form.cleaned_data["current_salary"], assumptions)
            new = calculate_clt_cost(form.cleaned_data["new_salary"], assumptions)
            result_dict = {
                "person_name": form.cleaned_data["person_name"],
                "current": current.as_dict(),
                "new": new.as_dict(),
                "monthly_diff": float(new.total_monthly - current.total_monthly),
                "annual_diff": float(new.total_annual - current.total_annual),
            }
            if request.POST.get("save_scenario"):
                Scenario.objects.create(
                    organization=org,
                    type=Scenario.Type.SALARY_ADJUST,
                    name=request.POST.get("scenario_name") or f"Ajuste {result_dict['person_name']}",
                    inputs={k: str(v) for k, v in form.cleaned_data.items()},
                    results=result_dict,
                    base_at=timezone.now(),
                )
    else:
        form = SalaryAdjustForm()
    return render(request, "scenarios/salary_adjust.html", {"form": form, "result": result_dict})


@login_required
@never_cache
def union_esp(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    assumptions = load_assumptions(org)
    result_dict = None
    if request.method == "POST":
        form = UnionEspForm(request.POST)
        if form.is_valid():
            # Aplica nova premissa de VA temporariamente
            tmp = dict(assumptions)
            tmp["clt_va_per_day"] = form.cleaned_data["va_per_day"]
            adjusted_salary = form.cleaned_data["avg_salary"] * (
                Decimal("1") + form.cleaned_data["salary_adjust_pct"] / 100
            )

            current = calculate_clt_cost(form.cleaned_data["avg_salary"], assumptions)
            new = calculate_clt_cost(adjusted_salary, tmp)
            headcount = form.cleaned_data["headcount"]

            result_dict = {
                "current_per_worker": current.as_dict(),
                "new_per_worker": new.as_dict(),
                "monthly_diff_total": float(
                    (new.total_monthly - current.total_monthly) * headcount
                ),
                "annual_diff_total": float(
                    (new.total_annual - current.total_annual) * headcount
                ),
                "headcount": headcount,
            }
            if request.POST.get("save_scenario"):
                Scenario.objects.create(
                    organization=org,
                    type=Scenario.Type.UNION_ESP,
                    name=request.POST.get("scenario_name") or "Sindicato ESP",
                    inputs={k: str(v) for k, v in form.cleaned_data.items()},
                    results=result_dict,
                    base_at=timezone.now(),
                )
    else:
        form = UnionEspForm()
    return render(request, "scenarios/union_esp.html", {"form": form, "result": result_dict})


@login_required
@never_cache
def compare(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    saved = Scenario.objects.filter(organization=org).order_by("-base_at")[:20]
    return render(request, "scenarios/compare.html", {"scenarios": saved})
