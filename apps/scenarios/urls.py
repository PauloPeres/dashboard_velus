"""URLs do bounded context Scenarios."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "scenarios"

urlpatterns = [
    path("", views.index, name="index"),
    path("pj-vs-clt/", views.pj_vs_clt, name="pj_vs_clt"),
    path("simples-split/", views.simples_split, name="simples_split"),
    path("salary-adjust/", views.salary_adjust, name="salary_adjust"),
    path("union-esp/", views.union_esp, name="union_esp"),
    path("compare/", views.compare, name="compare"),
]
