"""FakeNetworkElementSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import ElementGeometryDTO, NetworkElementDTO

_seed_elements: list[NetworkElementDTO] = []
_seed_geometries: list[ElementGeometryDTO] = []


class FakeNetworkElementSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.NETWORK_ELEMENTS})

    def __init__(self, **_credentials: Any) -> None:
        self._elements: list[NetworkElementDTO] = list(_seed_elements)
        self._geometries: list[ElementGeometryDTO] = list(_seed_geometries)

    @classmethod
    def set_seed(cls, elements: list[NetworkElementDTO]) -> None:
        global _seed_elements
        _seed_elements = list(elements)

    @classmethod
    def set_geometry_seed(cls, geometries: list[ElementGeometryDTO]) -> None:
        global _seed_geometries
        _seed_geometries = list(geometries)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_elements, _seed_geometries
        _seed_elements = []
        _seed_geometries = []

    def list_network_elements(self) -> Iterator[NetworkElementDTO]:
        yield from self._elements

    def list_element_geometries(self) -> Iterator[ElementGeometryDTO]:
        yield from self._geometries
