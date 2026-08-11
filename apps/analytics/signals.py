"""Signal listener — recomputa fact tables quando sync termina."""

from __future__ import annotations

from typing import Any

import structlog
from django.dispatch import receiver

from apps.sync.signals import sync_completed

from .application.churn_risk import compute_churn_risk_scores

_logger = structlog.get_logger(__name__)

# Capabilities cujos dados alimentam os sinais de risco de churn — só elas
# justificam recomputar o scoring após o sync (recompute é idempotente).
_CHURN_RELEVANT_CAPABILITIES = frozenset(
    {"CONTRACTS", "INVOICES", "TICKETS", "CONNECTIONS", "BANDWIDTH"}
)


@receiver(sync_completed)
def _on_sync_completed(
    sender: Any,  # noqa: ARG001
    organization: Any,
    capability: str,
    records_processed: int,
    **kwargs: Any,  # noqa: ARG001
) -> None:
    if records_processed == 0:
        return

    # O rebuild vai pra uma task própria (#132). Rodá-lo aqui significava
    # rematerializar as fact tables no mesmo processo que acabara de sincronizar:
    # num BOOTSTRAP de 111 mil faturas o worker morreu de OOM 3 segundos depois
    # de gravar COMPLETED, e o dashboard ficou desatualizado sem erro visível.
    from .tasks import rebuild_after_sync

    rebuild_after_sync.delay(
        organization_id=organization.pk, capability=capability
    )
    _logger.info(
        "analytics_rebuild_dispatched",
        organization=organization.slug,
        capability=capability,
        records_processed=records_processed,
    )

    if capability in _CHURN_RELEVANT_CAPABILITIES:
        risk_summary = compute_churn_risk_scores(organization)
        _logger.info(
            "churn_risk_recomputed_after_sync",
            organization=organization.slug,
            capability=capability,
            summary=risk_summary,
        )
