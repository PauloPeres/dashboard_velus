"""FakeCustomerSource — adapter in-memory pra testes.

Implementa `CustomerSourcePort` sem nenhuma chamada externa. Dados injetáveis
via construtor ou via método `set_data()` (útil pra fixtures).

Em testes:
    fake = FakeCustomerSource(customers=[CustomerDTO(...), ...])
    for dto in fake.list_customers():
        ...

Construtor aceita `**kwargs` adicionais que viriam de OrganizationDataSource
em ambiente real — todos ignorados (Fake não precisa de auth/url).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.customers.domain.dto import CustomerDTO
from apps.integrations.shared.enums import Capability, SourceType

# Storage in-memory PROCESSO-WIDE pra permitir setup em test fixtures que rodam
# antes do registry resolver o adapter. Em runtime de teste, fixture popula
# este dict via `FakeCustomerSource.set_seed(...)` e o sync vê os dados.
_seed_customers: list[CustomerDTO] = []


class FakeCustomerSource:
    """Adapter em memória — implementa `CustomerSourcePort`."""

    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.CUSTOMERS})

    def __init__(self, **_credentials: Any) -> None:
        # Aceita qualquer kwarg pra não quebrar quando OrganizationDataSource
        # tiver credenciais residuais — todos ignorados pelo Fake.
        self._customers: list[CustomerDTO] = list(_seed_customers)

    # -------------------------------------------------------------------------
    # Setup pra testes (class-level seed) e instance-level seed
    # -------------------------------------------------------------------------
    @classmethod
    def set_seed(cls, customers: list[CustomerDTO]) -> None:
        """Define o conjunto de clientes que TODA nova instância Fake verá.

        Útil em conftest.py — fixture chama `FakeCustomerSource.set_seed(...)`
        antes do sync rodar.
        """
        global _seed_customers
        _seed_customers = list(customers)

    @classmethod
    def reset_seed(cls) -> None:
        """Limpa o seed global — chame em teardown de teste pra isolar testes."""
        global _seed_customers
        _seed_customers = []

    def add_customer(self, dto: CustomerDTO) -> None:
        """Adiciona cliente só nesta instância (ignora seed global)."""
        self._customers.append(dto)

    # -------------------------------------------------------------------------
    # CustomerSourcePort
    # -------------------------------------------------------------------------
    def list_customers(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[CustomerDTO]:
        for dto in self._customers:
            if (
                since is not None
                and dto.created_at_source is not None
                and dto.created_at_source < since
            ):
                continue
            yield dto

    def get_customer(self, external_id: str) -> CustomerDTO | None:
        for dto in self._customers:
            if dto.external_id == external_id:
                return dto
        return None
