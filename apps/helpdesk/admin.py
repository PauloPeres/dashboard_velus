"""Admin de Ticket."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import Ticket


class _TenantAdminMixin:
    """Garante que org do user vai pro contextvar antes de qualquer query."""

    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(Ticket)
class TicketAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "protocol", "external_id", "status", "priority",
        "customer", "sector", "opened_at", "closed_at", "updated_at",
    )
    list_filter = ("source_type", "status", "priority", "sector")
    search_fields = ("protocol", "external_id", "customer__name", "customer_external_id", "message")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("customer",)
