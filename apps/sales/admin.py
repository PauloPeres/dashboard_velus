"""Admin de Lead e Opportunity."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import Lead, Opportunity


class _TenantAdminMixin:
    """Garante que org do user vai pro contextvar antes de qualquer query."""

    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(Lead)
class LeadAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "name", "external_id", "status", "origin",
        "salesperson_id", "created_at_source", "updated_at",
    )
    list_filter = ("source_type", "status", "origin")
    search_fields = ("name", "external_id", "phone", "email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Opportunity)
class OpportunityAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "external_id", "status", "value", "lead",
        "created_at_source", "closed_at", "updated_at",
    )
    list_filter = ("source_type", "status")
    search_fields = ("external_id", "lead_external_id", "loss_reason")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("lead",)
