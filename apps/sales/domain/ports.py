"""Ports do bounded context Sales — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import LeadDTO, OpportunityDTO


@runtime_checkable
class LeadSourcePort(Protocol):
    """Adapter que sabe ler leads/prospects de algum CRM externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_leads(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[LeadDTO]:
        """Itera leads. since=None -> bootstrap; senão incremental."""
        ...

    def get_lead(self, external_id: str) -> LeadDTO | None:
        """Busca lead único pelo ID na fonte externa."""
        ...


@runtime_checkable
class OpportunitySourcePort(Protocol):
    """Adapter que sabe ler negociações/oportunidades de algum CRM externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_opportunities(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[OpportunityDTO]:
        """Itera negociações. since=None -> bootstrap; senão incremental."""
        ...

    def get_opportunity(self, external_id: str) -> OpportunityDTO | None:
        """Busca negociação única pelo ID na fonte externa."""
        ...
