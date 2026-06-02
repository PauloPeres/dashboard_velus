"""Signal listener — recomputa fact tables quando sync termina."""

from __future__ import annotations

from typing import Any

import structlog
from django.dispatch import receiver

from apps.sync.signals import sync_completed

from .application.churn_risk import compute_churn_risk_scores
from .application.rebuild import rebuild_for_capability

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
    summary = rebuild_for_capability(organization, capability)
    _logger.info(
        "analytics_rebuild_done",
        organization=organization.slug,
        capability=capability,
        summary=summary,
    )

    if capability in _CHURN_RELEVANT_CAPABILITIES:
        risk_summary = compute_churn_risk_scores(organization)
        _logger.info(
            "churn_risk_recomputed_after_sync",
            organization=organization.slug,
            capability=capability,
            summary=risk_summary,
        )
