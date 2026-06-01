"""URLs do bounded context dashboards."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "dashboards"

urlpatterns = [
    path("", views.home, name="home"),
    path("executive/", views.executive, name="executive"),
    path("revenue/", views.revenue, name="revenue"),
    path("contracts/", views.contracts, name="contracts"),
    path("financial/", views.financial, name="financial"),
    path("financial/cashflow/", views.cashflow, name="cashflow"),
    path("financial/forecast/", views.forecast, name="forecast"),
    path("financial/dre/", views.dre, name="dre"),
    path("financial/dre-contas/", views.dre_detalhe, name="dre_detalhe"),
    path("financial/burn/", views.burn, name="burn"),
    path("financial/pessoas/", views.pessoas, name="pessoas"),
    path("churn/", views.churn, name="churn"),
    path("operations/", views.operations, name="operations"),
]
