"""Admin de ContractEquipment."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import ContractEquipment


class _TenantAdminMixin:
    """Garante que org do user vai pro contextvar antes de qualquer query."""

    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(ContractEquipment)
class ContractEquipmentAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "product_name", "external_id", "status", "contract",
        "serial", "mac", "value", "updated_at",
    )
    list_filter = ("source_type", "status")
    search_fields = (
        "product_name", "external_id", "serial", "mac",
        "contract_external_id", "contract__plan_name",
    )
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("contract",)
