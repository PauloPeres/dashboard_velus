"""DTOs neutros do bounded context Financial."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class InvoiceDTO:
    """Representação neutra de fatura/boleto.

    `contract_external_id` é opcional — algumas ISPs registram cobranças avulsas
    (taxa de adesão, instalação, multa) sem vínculo direto a um contrato.
    Repository resolve FK pra Contract se houver match; senão persiste com FK
    nula e mantém `contract_external_id` no campo string pra audit.
    """

    external_id: str
    contract_external_id: str  # pode ser "" pra cobrança avulsa
    amount: Decimal
    due_date: date
    status: str = "PENDING"  # PENDING | PAID | OVERDUE | CANCELED

    issued_at: datetime | None = None
    paid_at: datetime | None = None
    paid_amount: Decimal | None = None  # pode ser diferente do amount (juros/multa)

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("InvoiceDTO.external_id não pode ser vazio")
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
        if self.paid_amount is not None and not isinstance(self.paid_amount, Decimal):
            object.__setattr__(self, "paid_amount", Decimal(str(self.paid_amount)))


@dataclass(frozen=True)
class PaymentDTO:
    """Representação neutra de recebimento (movimento de caixa entrante)."""

    external_id: str
    invoice_external_id: str | None  # opcional — pagamento pode não estar atrelado
    contract_external_id: str | None
    amount: Decimal
    paid_at: datetime
    method: str = "UNKNOWN"  # BOLETO | PIX | TRANSFER | CASH | CARD | UNKNOWN

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("PaymentDTO.external_id não pode ser vazio")
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
