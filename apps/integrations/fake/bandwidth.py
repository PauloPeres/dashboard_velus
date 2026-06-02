"""FakeBandwidthUsageSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import BandwidthUsageDTO

_seed_usage: list[BandwidthUsageDTO] = []


class FakeBandwidthUsageSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.BANDWIDTH})

    def __init__(self, **_credentials: Any) -> None:
        self._usage: list[BandwidthUsageDTO] = list(_seed_usage)

    @classmethod
    def set_seed(cls, usage: list[BandwidthUsageDTO]) -> None:
        global _seed_usage
        _seed_usage = list(usage)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_usage
        _seed_usage = []

    def list_bandwidth_usage(
        self, *, since: datetime | None = None
    ) -> Iterator[BandwidthUsageDTO]:
        since_date = since.date() if since is not None else None
        for dto in self._usage:
            if (
                since_date is not None
                and dto.reference_date is not None
                and dto.reference_date < since_date
            ):
                continue
            yield dto

    def get_bandwidth_usage(self, external_id: str) -> BandwidthUsageDTO | None:
        for dto in self._usage:
            if dto.external_id == external_id:
                return dto
        return None
