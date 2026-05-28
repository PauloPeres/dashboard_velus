"""Admin de Customer.

OBS: TenantManager filtra automaticamente por org do contexto. O admin do Django
roda fora de request HTTP em alguns lugares (changelist via ORM direto), então
adicionamos uma camada via `get_queryset` que seta a org do user logado antes
de qualquer query.
"""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest
from simple_history.admin import SimpleHistoryAdmin

from apps.shared.context import set_current_organization

from .infrastructure.models import Customer


@admin.register(Customer)
class CustomerAdmin(SimpleHistoryAdmin):
    list_display = ("name", "document", "source_type", "external_id", "status", "updated_at")
    list_filter = ("source_type", "status")
    search_fields = ("name", "document", "email", "external_id")
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request: HttpRequest) -> Any:
        """Seta a org do user no contextvar antes do queryset rodar.

        Necessário porque admin acessa o queryset fora de middleware ativa
        em alguns code paths (autocomplete, lookups, etc.).
        """
        user = request.user
        if user.is_authenticated:
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                set_current_organization(get_org())
        return super().get_queryset(request)
