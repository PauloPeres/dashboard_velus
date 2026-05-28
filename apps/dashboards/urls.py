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
]
