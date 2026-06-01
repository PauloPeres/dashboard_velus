"""FakeConnectionSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import ConnectionDTO

_seed_connections: list[ConnectionDTO] = []


class FakeConnectionSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.CONNECTIONS})

    def __init__(self, **_credentials: Any) -> None:
        self._connections: list[ConnectionDTO] = list(_seed_connections)

    @classmethod
    def set_seed(cls, connections: list[ConnectionDTO]) -> None:
        global _seed_connections
        _seed_connections = list(connections)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_connections
        _seed_connections = []

    def list_connections(
        self, *, since: datetime | None = None
    ) -> Iterator[ConnectionDTO]:
        for dto in self._connections:
            if (
                since is not None
                and dto.last_connection_at is not None
                and dto.last_connection_at < since
            ):
                continue
            yield dto

    def get_connection(self, external_id: str) -> ConnectionDTO | None:
        for dto in self._connections:
            if dto.external_id == external_id:
                return dto
        return None
