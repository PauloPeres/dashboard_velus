"""URLs do bounded context sync — operação manual + status."""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "sync"

urlpatterns = [
    path("", views.status_page, name="status"),
    path("rows/", views.status_rows_partial, name="status_rows"),
    path("trigger/", views.trigger_sync, name="trigger"),
    path("trigger-all/", views.trigger_all_sync, name="trigger_all"),
]
