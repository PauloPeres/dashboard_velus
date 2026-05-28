"""FakeContractSource — adapter in-memory pra testes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.customers.domain.dto import ContractDTO
from apps.integrations.shared.enums import Capability, SourceType

_seed_contracts: list[ContractDTO] = []


class FakeContractSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.CONTRACTS})

    def __init__(self, **_credentials: Any) -> None:
        self._contracts: list[ContractDTO] = list(_seed_contracts)

    @classmethod
    def set_seed(cls, contracts: list[ContractDTO]) -> None:
        global _seed_contracts
        _seed_contracts = list(contracts)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_contracts
        _seed_contracts = []

    def list_contracts(self, *, since: datetime | None = None) -> Iterator[ContractDTO]:
        for dto in self._contracts:
            if (
                since is not None
                and dto.activated_at is not None
                and dto.activated_at < since
            ):
                continue
            yield dto

    def get_contract(self, external_id: str) -> ContractDTO | None:
        for dto in self._contracts:
            if dto.external_id == external_id:
                return dto
        return None
