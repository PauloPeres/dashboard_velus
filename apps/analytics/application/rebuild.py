"""Rebuild de dim/fact tables a partir do estado atual dos models de domínio.

Disparado por signal `sync_completed` — recomputa apenas o que mudou.
Em fase futura, isolar em tasks Celery agendadas com chunking pra orgs grandes.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.analytics.infrastructure.models import (
    DimContract,
    DimCustomer,
    DimPlan,
    FactContractStatusDaily,
    FactExpense,
    FactInvoice,
    FactPayment,
)
from apps.customers.infrastructure.models import Contract, Customer
from apps.financial.infrastructure.models import Expense, Invoice, Payment
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_ACTIVE_STATUSES = {"ACTIVE", "BLOCKED", "AWAITING_INSTALL"}


# =============================================================================
# SCD type 2 — upsert com versionamento
# =============================================================================
def _scd2_upsert_customer(customer: Customer) -> None:
    current = DimCustomer.objects.filter(
        organization=customer.organization,
        source_type=customer.source_type,
        external_id=customer.external_id,
        current=True,
    ).first()

    new_data = {
        "name": customer.name,
        "document": customer.document,
        "status": customer.status,
    }
    if current is not None:
        unchanged = all(getattr(current, k) == v for k, v in new_data.items())
        if unchanged:
            return
        current.current = False
        current.valid_to = timezone.now()
        current.save(update_fields=["current", "valid_to"])

    DimCustomer.objects.create(
        organization=customer.organization,
        source_type=customer.source_type,
        external_id=customer.external_id,
        valid_from=timezone.now(),
        current=True,
        **new_data,
    )


def _scd2_upsert_contract(contract: Contract) -> None:
    current = DimContract.objects.filter(
        organization=contract.organization,
        source_type=contract.source_type,
        external_id=contract.external_id,
        current=True,
    ).first()

    new_data = {
        "plan_name": contract.plan_name,
        "monthly_amount": contract.monthly_amount_net,
        "status": contract.status,
    }
    if current is not None:
        unchanged = all(getattr(current, k) == v for k, v in new_data.items())
        if unchanged:
            return
        current.current = False
        current.valid_to = timezone.now()
        current.save(update_fields=["current", "valid_to"])

    DimContract.objects.create(
        organization=contract.organization,
        source_type=contract.source_type,
        external_id=contract.external_id,
        valid_from=timezone.now(),
        current=True,
        **new_data,
    )

    # DimPlan — referência estável
    DimPlan.objects.get_or_create(
        organization=contract.organization,
        name=contract.plan_name,
        defaults={"monthly_amount": contract.monthly_amount_net},
    )


# =============================================================================
# Fact tables
# =============================================================================
def _aging_bucket(
    due_date: date,
    paid_at: Any,  # noqa: ARG001 — reservado pra futuro (data efetiva de pagamento)
    status: str,
    today: date,
) -> tuple[int, str]:
    """Retorna (days_overdue, aging_bucket) — usado em FactInvoice."""
    if status == "PAID":
        return 0, "PAID"
    if status == "CANCELED":
        return 0, "CANCELED"
    days = (today - due_date).days
    if days <= 0:
        return 0, "ON_TIME"
    if days <= 30:
        return days, "0_30"
    if days <= 60:
        return days, "31_60"
    if days <= 90:
        return days, "61_90"
    return days, "OVER_90"


_FACT_CONTRACT_BATCH = 10_000  # linhas por bulk_create batch


def _rebuild_fact_contract_status_daily(
    organization: Organization, since_date: date | None = None
) -> int:
    """Para cada Contract, gera snapshot diário até hoje (a partir de activated_at).

    Idempotente via UniqueConstraint (org, contract, date).
    Usa bulk_create com update_conflicts pra ser ~100x mais rápido do que
    update_or_create individual (crítico pra ISPs com 8k+ contratos × 400 dias).
    """
    today = timezone.now().date()
    start_default = today - timedelta(days=400)  # ~13 meses ringbuffer
    count = 0
    batch: list[FactContractStatusDaily] = []

    def _flush(records: list[FactContractStatusDaily]) -> int:
        if not records:
            return 0
        FactContractStatusDaily.objects.bulk_create(
            records,
            update_conflicts=True,
            unique_fields=["organization", "contract", "date"],
            update_fields=["status", "monthly_amount", "is_active"],
            batch_size=_FACT_CONTRACT_BATCH,
        )
        return len(records)

    contracts = Contract.objects.filter(organization=organization)
    for contract in contracts.iterator():
        contract_start = contract.activated_at.date() if contract.activated_at else start_default
        # Cap no início do ringbuffer pra não gerar anos de snapshots
        contract_start = max(contract_start, start_default)
        if since_date and since_date > contract_start:
            contract_start = since_date

        contract_end = contract.canceled_at.date() if contract.canceled_at else today
        if contract_end > today:
            contract_end = today
        if contract_start > today:
            continue

        is_active = contract.status in _ACTIVE_STATUSES
        # Status snapshot — usamos o status atual (simplificação MVP).
        # Refinamento futuro: ler de simple_history pra status histórico real.
        d = contract_start
        while d <= contract_end:
            batch.append(
                FactContractStatusDaily(
                    organization=organization,
                    contract=contract,
                    date=d,
                    status=contract.status,
                    monthly_amount=contract.monthly_amount_net,
                    is_active=is_active,
                )
            )
            d += timedelta(days=1)
            if len(batch) >= _FACT_CONTRACT_BATCH:
                count += _flush(batch)
                batch = []

    count += _flush(batch)
    return count


def _rebuild_fact_invoice(organization: Organization) -> int:
    today = timezone.now().date()
    count = 0
    for inv in Invoice.objects.filter(organization=organization).iterator():
        days_overdue, bucket = _aging_bucket(inv.due_date, inv.paid_at, inv.status, today)
        FactInvoice.objects.update_or_create(
            organization=organization,
            invoice=inv,
            defaults={
                "issued_date": (
                    inv.issued_at.date() if inv.issued_at else inv.due_date
                ),
                "due_date": inv.due_date,
                "paid_date": inv.paid_at.date() if inv.paid_at else None,
                "amount": inv.amount,
                "paid_amount": inv.paid_amount,
                "status": inv.status,
                "days_overdue": days_overdue,
                "aging_bucket": bucket,
            },
        )
        count += 1
    return count


def _rebuild_fact_payment(organization: Organization) -> int:
    count = 0
    for pay in Payment.objects.filter(organization=organization).iterator():
        FactPayment.objects.update_or_create(
            organization=organization,
            payment=pay,
            defaults={
                "paid_date": pay.paid_at.date(),
                "amount": pay.amount,
                "method": pay.method,
            },
        )
        count += 1
    return count


def _rebuild_fact_expense(organization: Organization) -> int:
    count = 0
    for exp in Expense.objects.filter(organization=organization).iterator():
        # expense_date = paid_at se pago, otherwise due_date
        expense_date = exp.paid_at if exp.paid_at else exp.due_date
        FactExpense.objects.update_or_create(
            organization=organization,
            expense=exp,
            defaults={
                "expense_date": expense_date,
                "due_date": exp.due_date,
                "paid_date": exp.paid_at,
                "amount": exp.amount,
                "paid_amount": exp.paid_amount,
                "status": exp.status,
                "category": exp.category,
                "supplier_name": exp.supplier_name,
                "description": exp.description,
            },
        )
        count += 1
    return count


# =============================================================================
# Entrypoints públicos (chamados pelo signal listener)
# =============================================================================
@allow_cross_tenant(reason="analytics rebuild itera models de domínio sem context HTTP")
@transaction.atomic
def rebuild_for_capability(organization: Organization, capability: str) -> dict[str, int]:
    """Recomputa dim/fact afetadas por uma capability sincronizada."""
    summary: dict[str, int] = {}

    if capability == "CUSTOMERS":
        for cust in Customer.objects.filter(organization=organization).iterator():
            _scd2_upsert_customer(cust)
        summary["dim_customer"] = DimCustomer.objects.filter(
            organization=organization, current=True
        ).count()

    elif capability == "CONTRACTS":
        for ctr in Contract.objects.filter(organization=organization).iterator():
            _scd2_upsert_contract(ctr)
        summary["dim_contract"] = DimContract.objects.filter(
            organization=organization, current=True
        ).count()
        summary["fact_status_daily"] = _rebuild_fact_contract_status_daily(organization)

    elif capability == "INVOICES":
        summary["fact_invoice"] = _rebuild_fact_invoice(organization)

    elif capability == "PAYMENTS":
        summary["fact_payment"] = _rebuild_fact_payment(organization)

    elif capability == "EXPENSES":
        summary["fact_expense"] = _rebuild_fact_expense(organization)

    return summary
