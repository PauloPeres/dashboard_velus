"""Ports do bounded context Network — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import ConnectionDTO


@runtime_checkable
class ConnectionSourcePort(Protocol):
    """Adapter que sabe ler estado de conexão (RADIUS) de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_connections(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[ConnectionDTO]:
        """Itera conexões. since=None -> bootstrap; senão incremental."""
        ...

    def get_connection(self, external_id: str) -> ConnectionDTO | None:
        """Busca conexão única pelo ID na fonte externa."""
        ...
