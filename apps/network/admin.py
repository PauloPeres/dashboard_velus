"""Admin de Connection."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import (
    BandwidthUsage,
    Connection,
    ConnectionDropEvent,
    ConnectionPollState,
    NetworkElement,
    OutageAffectedLogin,
    OutageEvent,
)


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


@admin.register(NetworkElement)
class NetworkElementAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = (
        "kind", "external_id", "name", "parent_kind", "parent_external_id",
        "capacity", "project_external_id", "status", "updated_at",
    )
    list_filter = ("source_type", "kind", "project_external_id", "status")
    search_fields = ("external_id", "name", "address", "parent_external_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ConnectionDropEvent)
class ConnectionDropEventAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = (
        "login", "dropped_at", "restored_at", "reason",
        "cto_external_id", "cto_port", "pon_external_id", "monthly_amount",
    )
    list_filter = ("reason", "dropped_at")
    search_fields = ("login", "cto_external_id", "customer__name")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("customer",)
    date_hierarchy = "dropped_at"


@admin.register(OutageEvent)
class OutageEventAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = (
        "started_at", "ended_at", "scope", "element_label",
        "suspected_segment_label", "confidence",
        "affected_count", "restored_count", "mrr_at_risk",
    )
    list_filter = ("scope", "confidence")
    search_fields = ("element_external_id", "element_label", "suspected_segment_label")
    readonly_fields = ("created_at", "updated_at")


@admin.register(OutageAffectedLogin)
class OutageAffectedLoginAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = ("login", "outage", "dropped_at", "restored_at", "monthly_amount")
    search_fields = ("login",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ConnectionPollState)
class ConnectionPollStateAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = ("organization", "baseline_at", "last_poll_at", "last_success_at")
    readonly_fields = ("created_at", "updated_at")
