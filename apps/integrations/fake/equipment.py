"""FakeEquipmentSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.inventory.domain.dto import EquipmentDTO

_seed_equipment: list[EquipmentDTO] = []


class FakeEquipmentSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.EQUIPMENT})

    def __init__(self, **_credentials: Any) -> None:
        self._equipment: list[EquipmentDTO] = list(_seed_equipment)

    @classmethod
    def set_seed(cls, equipment: list[EquipmentDTO]) -> None:
        global _seed_equipment
        _seed_equipment = list(equipment)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_equipment
        _seed_equipment = []

    def list_equipment(
        self, *, since: datetime | None = None
    ) -> Iterator[EquipmentDTO]:
        yield from self._equipment

    def get_equipment(self, external_id: str) -> EquipmentDTO | None:
        for dto in self._equipment:
            if dto.external_id == external_id:
                return dto
        return None
