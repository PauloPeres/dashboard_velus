"""URLs do bounded context dashboards."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "dashboards"

urlpatterns = [
    path("", views.home, name="home"),
    path("executive/", views.executive, name="executive"),
    path("revenue/", views.revenue, name="revenue"),
    path("financial/", views.financial, name="financial"),
    path("financial/cashflow/", views.cashflow, name="cashflow"),
    path("financial/forecast/", views.forecast, name="forecast"),
    path("financial/dre/", views.dre, name="dre"),
    path("financial/burn/", views.burn, name="burn"),
]
