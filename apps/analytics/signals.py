"""Signal listener — recomputa fact tables quando sync termina."""

from __future__ import annotations

from typing import Any

import structlog
from django.dispatch import receiver

from apps.sync.signals import sync_completed

from .application.rebuild import rebuild_for_capability

_logger = structlog.get_logger(__name__)


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
