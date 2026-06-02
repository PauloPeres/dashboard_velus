"""DTOs do domínio Sales/CRM — neutros, sem campos source-specific."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class LeadDTO:
    """Representação neutra de um lead/prospect do funil de vendas.

    `external_id` é opaco — string que identifica o candidato no sistema de
    origem. Combinado com `source_type` forma a chave composta de persistência:
    `(organization, source_type, external_id)`.

    `status` deriva do estágio de prospecção na origem:
    NEW, CONTACTED, CONVERTED, LOST, UNKNOWN.
    """

    external_id: str
    name: str
    status: str  # NEW, CONTACTED, CONVERTED, LOST, UNKNOWN

    phone: str = ""
    email: str = ""
    origin: str = ""  # canal de origem (indicação, site, redes sociais...)
    salesperson_id: str = ""
    created_at_source: datetime | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("LeadDTO.external_id não pode ser vazio")


@dataclass(frozen=True)
class OpportunityDTO:
    """Representação neutra de uma negociação/oportunidade de venda.

    `lead_external_id` é o snapshot do lead dono da negociação — o Repository
    tenta resolver a FK via `(organization, source_type, lead_external_id)`.

    `status` deriva do estado da negociação na origem:
    OPEN (em andamento), WON (ganha), LOST (perdida), UNKNOWN.
    """

    external_id: str
    lead_external_id: str
    status: str  # OPEN, WON, LOST, UNKNOWN

    value: Decimal = Decimal("0")
    loss_reason: str = ""
    created_at_source: datetime | None = None
    closed_at: datetime | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("OpportunityDTO.external_id não pode ser vazio")
