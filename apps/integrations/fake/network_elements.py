"""FakeNetworkElementSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import NetworkElementDTO

_seed_elements: list[NetworkElementDTO] = []


class FakeNetworkElementSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.NETWORK_ELEMENTS})

    def __init__(self, **_credentials: Any) -> None:
        self._elements: list[NetworkElementDTO] = list(_seed_elements)

    @classmethod
    def set_seed(cls, elements: list[NetworkElementDTO]) -> None:
        global _seed_elements
        _seed_elements = list(elements)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_elements
        _seed_elements = []

    def list_network_elements(self) -> Iterator[NetworkElementDTO]:
        yield from self._elements
