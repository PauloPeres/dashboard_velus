"""FakeOpportunitySource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.sales.domain.dto import OpportunityDTO

_seed_opportunities: list[OpportunityDTO] = []


class FakeOpportunitySource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.OPPORTUNITIES})

    def __init__(self, **_credentials: Any) -> None:
        self._opportunities: list[OpportunityDTO] = list(_seed_opportunities)

    @classmethod
    def set_seed(cls, opportunities: list[OpportunityDTO]) -> None:
        global _seed_opportunities
        _seed_opportunities = list(opportunities)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_opportunities
        _seed_opportunities = []

    def list_opportunities(
        self, *, since: datetime | None = None
    ) -> Iterator[OpportunityDTO]:
        for dto in self._opportunities:
            if (
                since is not None
                and dto.created_at_source is not None
                and dto.created_at_source < since
            ):
                continue
            yield dto

    def get_opportunity(self, external_id: str) -> OpportunityDTO | None:
        for dto in self._opportunities:
            if dto.external_id == external_id:
                return dto
        return None
