"""FakeLeadSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.sales.domain.dto import LeadDTO

_seed_leads: list[LeadDTO] = []


class FakeLeadSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.LEADS})

    def __init__(self, **_credentials: Any) -> None:
        self._leads: list[LeadDTO] = list(_seed_leads)

    @classmethod
    def set_seed(cls, leads: list[LeadDTO]) -> None:
        global _seed_leads
        _seed_leads = list(leads)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_leads
        _seed_leads = []

    def list_leads(self, *, since: datetime | None = None) -> Iterator[LeadDTO]:
        for dto in self._leads:
            if (
                since is not None
                and dto.created_at_source is not None
                and dto.created_at_source < since
            ):
                continue
            yield dto

    def get_lead(self, external_id: str) -> LeadDTO | None:
        for dto in self._leads:
            if dto.external_id == external_id:
                return dto
        return None
