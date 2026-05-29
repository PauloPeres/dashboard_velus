"""FakeExpenseSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.financial.domain.dto import ExpenseDTO
from apps.integrations.shared.enums import Capability, SourceType

_seed_expenses: list[ExpenseDTO] = []


class FakeExpenseSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.EXPENSES})

    def __init__(self, **_credentials: Any) -> None:
        self._expenses: list[ExpenseDTO] = list(_seed_expenses)

    @classmethod
    def set_seed(cls, expenses: list[ExpenseDTO]) -> None:
        global _seed_expenses
        _seed_expenses = list(expenses)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_expenses
        _seed_expenses = []

    def list_expenses(self, *, since: datetime | None = None) -> Iterator[ExpenseDTO]:
        for dto in self._expenses:
            if (
                since is not None
                and dto.issued_at is not None
                and datetime(dto.issued_at.year, dto.issued_at.month, dto.issued_at.day) < since
            ):
                continue
            yield dto
