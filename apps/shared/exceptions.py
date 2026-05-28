"""Exceções do kernel compartilhado."""

from __future__ import annotations


class NoOrganizationInContextError(RuntimeError):
    """Levantada quando há tentativa de query em TenantModel sem org no contexto.

    Causas comuns:
    - Middleware não está montado (request HTTP sem TenantMiddleware)
    - Task Celery não setou `set_current_organization` antes da query
    - Management command rodando sem `@allow_cross_tenant`
    """


class CrossTenantAccessError(RuntimeError):
    """Levantada em tentativa de bypass do TenantManager sem `@allow_cross_tenant`.

    Defesa contra bug humano: se código tenta acessar dados sem escopo,
    falha alto e claro em vez de retornar dados de OUTROS tenants.
    """
