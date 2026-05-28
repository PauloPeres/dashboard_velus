"""Admin de Scenarios."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.scenarios.infrastructure.models import Assumption, Scenario
from apps.shared.context import set_current_organization


class _TenantAdminMixin:
    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(Assumption)
class AssumptionAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = ("key", "value", "unit", "description")
    list_filter = ("unit",)
    search_fields = ("key", "description")
    list_editable = ("value",)
    ordering = ("key",)


@admin.register(Scenario)
class ScenarioAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = ("name", "type", "base_at")
    list_filter = ("type",)
    search_fields = ("name", "description")
    readonly_fields = ("base_at",)
