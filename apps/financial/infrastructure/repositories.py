"""Repositórios do Financial — upsert idempotente, resolve FKs cross-context."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.customers.infrastructure.models import Contract
from apps.financial.domain.dto import ExpenseDTO, InvoiceDTO, PaymentDTO
from apps.integrations.shared.enums import SourceType
from apps.tenancy.models import Organization

from .models import Expense, Invoice, Payment


def _soft_delete_missing(
    model: Any,
    organization: Organization,
    *,
    seen_external_ids: set[str],
    source_type: SourceType,
    min_keep_ratio: float,
) -> dict[str, Any]:
    """Marca como soft-deleted os registros ativos cujo external_id NÃO veio no
    pull completo do IXC (foram removidos na fonte da verdade) e reativa os que
    voltaram a aparecer. Genérico — serve Payment e Expense.

    Detecção por ausência: opera sobre o conjunto local ativo vs. o conjunto
    visto no pull. Conjuntos `to_delete`/`to_reactivate` são pequenos (só o
    delta), então os UPDATEs usam IN curto — sem IN gigante nem re-save linha a
    linha (que estouraria as tabelas de history).

    Guard-rail: se o pull trouxe menos que `min_keep_ratio` do total ativo
    local, ABORTA sem deletar — protege contra um pull parcial/falho zerar tudo.
    """
    base = model.objects.filter(
        organization=organization, source_type=source_type.value
    )
    active_ids = set(
        base.filter(deleted_at__isnull=True).values_list("external_id", flat=True)
    )

    if active_ids and len(seen_external_ids) < len(active_ids) * min_keep_ratio:
        return {
            "active": len(active_ids),
            "seen": len(seen_external_ids),
            "soft_deleted": 0,
            "reactivated": 0,
            "aborted": True,
        }

    now = timezone.now()
    to_delete = active_ids - seen_external_ids
    soft_deleted = (
        base.filter(external_id__in=to_delete).update(deleted_at=now)
        if to_delete
        else 0
    )

    deleted_ids = set(
        base.filter(deleted_at__isnull=False).values_list("external_id", flat=True)
    )
    to_reactivate = deleted_ids & seen_external_ids
    reactivated = (
        base.filter(external_id__in=to_reactivate).update(deleted_at=None)
        if to_reactivate
        else 0
    )

    return {
        "active": len(active_ids),
        "seen": len(seen_external_ids),
        "soft_deleted": soft_deleted,
        "reactivated": reactivated,
        "aborted": False,
    }


class InvoiceRepository:
    """Upsert idempotente de Invoice. Resolve FK pra Contract por external_id."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: InvoiceDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Invoice, bool]:
        contract = (
            Contract.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.contract_external_id,
            )
            .first()
        )

        defaults: dict[str, Any] = {
            "contract": contract,
            "contract_external_id": dto.contract_external_id,
            "amount": dto.amount,
            "due_date": dto.due_date,
            "status": self._normalize_status(dto.status),
            "issued_at": dto.issued_at,
            "paid_at": dto.paid_at,
            "paid_amount": dto.paid_amount,
            "raw_extras": dto.raw_extras,
        }
        invoice, created = Invoice.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return invoice, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Invoice.Status.values:
            return raw_upper
        return Invoice.Status.UNKNOWN.value


class PaymentRepository:
    """Upsert idempotente de Payment. Resolve FKs opcionais."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: PaymentDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Payment, bool]:
        invoice = None
        if dto.invoice_external_id:
            invoice = (
                Invoice.objects
                .filter(
                    organization=self.organization,
                    source_type=source_type.value,
                    external_id=dto.invoice_external_id,
                )
                .first()
            )

        contract = None
        if dto.contract_external_id:
            contract = (
                Contract.objects
                .filter(
                    organization=self.organization,
                    source_type=source_type.value,
                    external_id=dto.contract_external_id,
                )
                .first()
            )

        defaults: dict[str, Any] = {
            "invoice": invoice,
            "contract": contract,
            "invoice_external_id": dto.invoice_external_id or "",
            "contract_external_id": dto.contract_external_id or "",
            "amount": dto.amount,
            "paid_at": dto.paid_at,
            "method": self._normalize_method(dto.method),
            "raw_extras": dto.raw_extras,
            # Reativa: se voltou a aparecer no IXC, deixa de estar soft-deleted.
            "deleted_at": None,
        }
        payment, created = Payment.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return payment, created

    def soft_delete_missing(
        self,
        *,
        seen_external_ids: set[str],
        source_type: SourceType,
        min_keep_ratio: float = 0.5,
    ) -> dict[str, Any]:
        """Soft-delete de Payments ausentes no pull completo (ver _soft_delete_missing)."""
        return _soft_delete_missing(
            Payment,
            self.organization,
            seen_external_ids=seen_external_ids,
            source_type=source_type,
            min_keep_ratio=min_keep_ratio,
        )

    @staticmethod
    def _normalize_method(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Payment.Method.values:
            return raw_upper
        return Payment.Method.UNKNOWN.value


class ExpenseRepository:
    """Upsert idempotente de Expense."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: ExpenseDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Expense, bool]:
        defaults: dict[str, Any] = {
            "supplier_name": dto.supplier_name,
            "supplier_external_id": dto.supplier_external_id,
            "description": dto.description,
            "category": dto.category,
            "amount": dto.amount,
            "paid_amount": dto.paid_amount,
            "issued_at": dto.issued_at,
            "due_date": dto.due_date,
            "paid_at": dto.paid_at,
            "status": self._normalize_status(dto.status),
            "payment_type": dto.payment_type,
            "raw_extras": dto.raw_extras,
            # Reativa: se voltou a aparecer no IXC, deixa de estar soft-deleted.
            "deleted_at": None,
        }
        expense, created = Expense.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return expense, created

    def soft_delete_missing(
        self,
        *,
        seen_external_ids: set[str],
        source_type: SourceType,
        min_keep_ratio: float = 0.5,
    ) -> dict[str, Any]:
        """Soft-delete de Expenses ausentes no pull completo (ver _soft_delete_missing)."""
        return _soft_delete_missing(
            Expense,
            self.organization,
            seen_external_ids=seen_external_ids,
            source_type=source_type,
            min_keep_ratio=min_keep_ratio,
        )

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Expense.Status.values:
            return raw_upper
        return Expense.Status.UNKNOWN.value
