"""Contextvars que carregam a organização atual da request/task.

Por que contextvar (e não threading.local)?
- Async-safe: funciona em Django async views e em código asyncio (httpx async).
- Celery 5 com asyncio + workers gevent: contextvar isola por task.

Uso:
    HTTP:    middleware (apps.shared.middleware.TenantMiddleware) seta automaticamente.
    Celery:  task SEMPRE recebe organization_id em kwargs e chama set_current_organization.
    Tests:   fixtures setam manualmente.
    Admin:   decorator/mixin de Admin set ao ler request.user.organization.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenancy.models import Organization

_current_organization: contextvars.ContextVar[Organization | None] = contextvars.ContextVar(
    "velus_current_organization", default=None
)

# Flag interno para bypass via @allow_cross_tenant — NÃO usar diretamente.
_allow_cross_tenant: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "velus_allow_cross_tenant", default=False
)


def get_current_organization() -> Organization | None:
    """Retorna a organização atual do contexto, ou None se não setada."""
    return _current_organization.get()


def set_current_organization(org: Organization | None) -> contextvars.Token:
    """Seta a organização atual e devolve um token pra restaurar depois."""
    return _current_organization.set(org)


def reset_current_organization(token: contextvars.Token) -> None:
    """Restaura o valor anterior do contextvar (chamar em finally)."""
    _current_organization.reset(token)


def is_cross_tenant_allowed() -> bool:
    """True se @allow_cross_tenant estiver ativo no contexto atual."""
    return _allow_cross_tenant.get()


def _set_cross_tenant_flag(value: bool) -> contextvars.Token:
    """Uso interno do decorator allow_cross_tenant — não chamar em código de app."""
    return _allow_cross_tenant.set(value)


def _reset_cross_tenant_flag(token: contextvars.Token) -> None:
    """Uso interno do decorator allow_cross_tenant."""
    _allow_cross_tenant.reset(token)
