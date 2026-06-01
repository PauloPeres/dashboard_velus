"""DTOs do dominio Helpdesk — neutros, sem campos source-specific."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class TicketDTO:
    """Representacao neutra de um chamado de suporte vindo de qualquer fonte externa.

    `external_id` e opaco — string que identifica o chamado no sistema de origem.
    Combinado com `source_type` (que vive no adapter, nao aqui), forma a chave
    composta de persistencia: `(organization, source_type, external_id)`.
    """

    external_id: str
    customer_external_id: str
    subject_id: str
    sector: str
    technician_id: str
    status: str  # OPEN, SCHEDULED, IN_PROGRESS, CLOSED, FORWARDED
    priority: str  # NORMAL, HIGH, LOW, URGENT
    message: str
    protocol: str
    opened_at: datetime

    scheduled_at: datetime | None = None
    closed_at: datetime | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("TicketDTO.external_id nao pode ser vazio")
