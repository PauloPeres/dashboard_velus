"""Admin de Connection."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import BandwidthUsage, Connection


class _TenantAdminMixin:
    """Garante que org do user vai pro contextvar antes de qualquer query."""

    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(Connection)
class ConnectionAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "login", "external_id", "status", "customer",
        "ip", "nas_ip", "last_connection_at", "updated_at",
    )
    list_filter = ("source_type", "status", "nas_ip")
    search_fields = (
        "login", "external_id", "customer__name",
        "customer_external_id", "ip", "nas_ip",
    )
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("customer",)


@admin.register(BandwidthUsage)
class BandwidthUsageAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "external_id", "customer", "customer_external_id",
        "download_bytes", "upload_bytes", "session_time",
        "reference_date", "updated_at",
    )
    list_filter = ("source_type", "reference_date")
    search_fields = (
        "external_id", "customer__name", "customer_external_id",
    )
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("customer",)
