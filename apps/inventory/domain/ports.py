"""Ports do bounded context Inventory — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import EquipmentDTO


@runtime_checkable
class EquipmentSourcePort(Protocol):
    """Adapter que sabe ler equipamentos em comodato de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_equipment(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[EquipmentDTO]:
        """Itera equipamentos. since=None -> bootstrap; senão incremental."""
        ...

    def get_equipment(self, external_id: str) -> EquipmentDTO | None:
        """Busca equipamento único pelo ID na fonte externa."""
        ...
