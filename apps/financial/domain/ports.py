"""Ports do bounded context Financial."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import InvoiceDTO, PaymentDTO


@runtime_checkable
class InvoiceSourcePort(Protocol):
    """Adapter que sabe ler faturas/boletos de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_invoices(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[InvoiceDTO]:
        """Itera faturas. since=None → bootstrap; senão incremental."""
        ...


@runtime_checkable
class PaymentSourcePort(Protocol):
    """Adapter que sabe ler pagamentos recebidos de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_payments(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[PaymentDTO]:
        """Itera pagamentos. since=None → bootstrap; senão incremental."""
        ...
