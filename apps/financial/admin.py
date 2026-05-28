"""Admin do Financial."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import Invoice, Payment


class _TenantAdminMixin:
    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(Invoice)
class InvoiceAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "external_id", "amount", "due_date", "status", "contract",
        "source_type", "paid_at",
    )
    list_filter = ("status", "source_type", "due_date")
    search_fields = ("external_id", "contract__external_id", "contract_external_id")
    autocomplete_fields = ("contract",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(Payment)
class PaymentAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "external_id", "amount", "paid_at", "method",
        "invoice", "contract", "source_type",
    )
    list_filter = ("method", "source_type", "paid_at")
    search_fields = ("external_id", "invoice__external_id", "contract__external_id")
    autocomplete_fields = ("invoice", "contract")
    readonly_fields = ("created_at", "updated_at")
