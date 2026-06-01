"""FakeTicketSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.helpdesk.domain.dto import TicketDTO
from apps.integrations.shared.enums import Capability, SourceType

_seed_tickets: list[TicketDTO] = []


class FakeTicketSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.TICKETS})

    def __init__(self, **_credentials: Any) -> None:
        self._tickets: list[TicketDTO] = list(_seed_tickets)

    @classmethod
    def set_seed(cls, tickets: list[TicketDTO]) -> None:
        global _seed_tickets
        _seed_tickets = list(tickets)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_tickets
        _seed_tickets = []

    def list_tickets(self, *, since: datetime | None = None) -> Iterator[TicketDTO]:
        for dto in self._tickets:
            if (
                since is not None
                and dto.opened_at is not None
                and dto.opened_at < since
            ):
                continue
            yield dto

    def get_ticket(self, external_id: str) -> TicketDTO | None:
        for dto in self._tickets:
            if dto.external_id == external_id:
                return dto
        return None
