"""Managers que aplicam isolamento por tenant automaticamente."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import models

from .context import get_current_organization, is_cross_tenant_allowed
from .exceptions import NoOrganizationInContextError

if TYPE_CHECKING:
    pass


class TenantManager(models.Manager):
    """Manager que filtra automaticamente por organização do contexto.

    Comportamento:
    - Se houver organização no contexto: queryset filtrado por ela.
    - Se NÃO houver E `@allow_cross_tenant` ativo: queryset SEM filtro (bypass).
    - Se NÃO houver E sem decorator: levanta NoOrganizationInContextError.

    Decisões de design:
    - Não expomos `unscoped()` separado — bypass é exclusivamente via decorator,
      garantindo audit log de toda chamada cross-tenant.
    - Falha alto quando contexto vazio (vs. retornar empty queryset silencioso)
      pra evitar bugs sutis "por que sumiu o dado".
    """

    def get_queryset(self) -> models.QuerySet:
        if is_cross_tenant_allowed():
            return super().get_queryset()

        org = get_current_organization()
        if org is None:
            raise NoOrganizationInContextError(
                f"Query em {self.model.__name__} sem organização no contexto. "
                "Use TenantMiddleware (HTTP), set_current_organization (Celery) "
                "ou @allow_cross_tenant (operações administrativas)."
            )
        return super().get_queryset().filter(organization=org)
