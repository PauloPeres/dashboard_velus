"""FakeInvoiceSource — adapter in-memory pra testes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.financial.domain.dto import InvoiceDTO, PaymentDTO
from apps.integrations.shared.enums import Capability, SourceType

_seed_invoices: list[InvoiceDTO] = []
_seed_payments: list[PaymentDTO] = []


class FakeInvoiceSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.INVOICES})

    def __init__(self, **_credentials: Any) -> None:
        self._invoices: list[InvoiceDTO] = list(_seed_invoices)

    @classmethod
    def set_seed(cls, invoices: list[InvoiceDTO]) -> None:
        global _seed_invoices
        _seed_invoices = list(invoices)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_invoices
        _seed_invoices = []

    def list_invoices(self, *, since: datetime | None = None) -> Iterator[InvoiceDTO]:
        for dto in self._invoices:
            if (
                since is not None
                and dto.issued_at is not None
                and dto.issued_at < since
            ):
                continue
            yield dto


class FakePaymentSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.PAYMENTS})

    def __init__(self, **_credentials: Any) -> None:
        self._payments: list[PaymentDTO] = list(_seed_payments)

    @classmethod
    def set_seed(cls, payments: list[PaymentDTO]) -> None:
        global _seed_payments
        _seed_payments = list(payments)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_payments
        _seed_payments = []

    def list_payments(self, *, since: datetime | None = None) -> Iterator[PaymentDTO]:
        for dto in self._payments:
            if since is not None and dto.paid_at < since:
                continue
            yield dto
