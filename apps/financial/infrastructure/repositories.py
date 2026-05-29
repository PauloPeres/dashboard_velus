"""Repositórios do Financial — upsert idempotente, resolve FKs cross-context."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.customers.infrastructure.models import Contract
from apps.financial.domain.dto import ExpenseDTO, InvoiceDTO, PaymentDTO
from apps.integrations.shared.enums import SourceType
from apps.tenancy.models import Organization

from .models import Expense, Invoice, Payment


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
        }
        payment, created = Payment.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return payment, created

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
        }
        expense, created = Expense.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return expense, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Expense.Status.values:
            return raw_upper
        return Expense.Status.UNKNOWN.value
