"""Ports do bounded context Helpdesk — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import TicketDTO


@runtime_checkable
class TicketSourcePort(Protocol):
    """Adapter que sabe ler chamados de suporte de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_tickets(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[TicketDTO]:
        """Itera chamados. since=None -> bootstrap; senao incremental."""
        ...

    def get_ticket(self, external_id: str) -> TicketDTO | None:
        """Busca chamado unico pelo ID na fonte externa."""
        ...
