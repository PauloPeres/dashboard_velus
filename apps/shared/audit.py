"""Helpers de audit log."""

from __future__ import annotations

import structlog

_logger = structlog.get_logger(__name__)


def log_cross_tenant_access(module: str, qualname: str, reason: str) -> None:
    """Registra uso explícito de @allow_cross_tenant.

    Vai pra log estruturado (JSON em prod). Em prod, hook de SIEM/Sentry pode
    alertar em uso excessivo ou em horário fora do esperado.
    """
    _logger.warning(
        "cross_tenant_access",
        module=module,
        function=qualname,
        reason=reason,
    )
