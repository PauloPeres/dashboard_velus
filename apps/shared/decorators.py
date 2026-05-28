"""Decorators do kernel compartilhado."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from .audit import log_cross_tenant_access
from .context import _reset_cross_tenant_flag, _set_cross_tenant_flag

P = ParamSpec("P")
T = TypeVar("T")


def allow_cross_tenant(reason: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Libera bypass do TenantManager dentro do escopo da função decorada.

    Uso restrito a operações administrativas legítimas:
    - Management commands que iteram entre orgs (sync orchestrator)
    - Cálculos de telemetria global (contagem total de clientes para health check)
    - Migrações de dados que precisam tocar várias orgs

    Cada uso é registrado em log estruturado (ver `audit.log_cross_tenant_access`).
    Em produção, hook de SIEM/alerta pode disparar em uso fora do esperado.

    Exemplo:
        @allow_cross_tenant(reason="iterar todas as orgs no sync agendado")
        def dispatch_sync_for_all_orgs() -> None:
            for org in Organization.objects.all():
                ...
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            log_cross_tenant_access(
                module=func.__module__,
                qualname=func.__qualname__,
                reason=reason,
            )
            token = _set_cross_tenant_flag(True)
            try:
                return func(*args, **kwargs)
            finally:
                _reset_cross_tenant_flag(token)

        return wrapper

    return decorator
