"""Admin de Atendimento."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import Atendimento, Departamento, Mensagem


class _TenantAdminMixin:
    """Garante que org do user vai pro contextvar antes de qualquer query."""

    def get_queryset(self, request: HttpRequest) -> Any:  # type: ignore[override]
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)  # type: ignore[misc]


@admin.register(Departamento)
class DepartamentoAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = ("nome", "external_id", "status", "source_type", "updated_at")
    list_filter = ("source_type", "status")
    search_fields = ("nome", "external_id")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Atendimento)
class AtendimentoAdmin(_TenantAdminMixin, SimpleHistoryAdmin):
    list_display = (
        "protocol", "external_id", "status", "canal",
        "customer", "departamento", "atendente_nome",
        "rating", "opened_at", "closed_at",
    )
    list_filter = ("source_type", "status", "canal")
    search_fields = (
        "protocol", "external_id", "customer__name",
        "customer_document", "customer_external_id",
    )
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ("customer", "departamento")


@admin.register(Mensagem)
class MensagemAdmin(_TenantAdminMixin, admin.ModelAdmin):
    list_display = (
        "external_id", "atendimento_external_id", "direction",
        "tipo", "sent_at",
    )
    list_filter = ("source_type", "direction", "tipo")
    search_fields = ("external_id", "atendimento_external_id", "texto")
    readonly_fields = ("created_at", "updated_at")
