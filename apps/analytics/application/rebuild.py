"""Rebuild de dim/fact tables a partir do estado atual dos models de domínio.

Disparado por signal `sync_completed` — recomputa apenas o que mudou.
Em fase futura, isolar em tasks Celery agendadas com chunking pra orgs grandes.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
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


_ZERO = Decimal("0")
# Campos de encargo por atraso no fn_areceber do IXC (vão pra raw_extras).
_LATE_FEE_KEYS = ("valor_multas", "valor_juros")


def _to_decimal(value: Any) -> Decimal:
    """Converte string/num do IXC (vírgula decimal, vazio, None) em Decimal."""
    if value in (None, "", "0", "0.00"):
        return _ZERO
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return _ZERO


def _parse_late_fee(raw_extras: Any) -> Decimal:
    """Soma multa + juros do `raw_extras` da fatura IXC (função pura).

    Fallback gracioso: origem sem esses campos (ou zerados) → 0. O IXC só
    materializa multa/juros no pagamento/reemissão, então faturas em aberto
    normalmente retornam 0 aqui.
    """
    if not isinstance(raw_extras, dict):
        return _ZERO
    total = _ZERO
    for key in _LATE_FEE_KEYS:
        total += _to_decimal(raw_extras.get(key))
    return total


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


# Linhas por batch nos rebuilds de fact financeiras (bulk_create upsert).
_FACT_FINANCIAL_BATCH = 5_000


def _rebuild_fact_invoice(organization: Organization) -> int:
    """Materializa FactInvoice a partir de Invoice (idempotente).

    Usa bulk_create com update_conflicts em batches — ~100x mais rápido que
    update_or_create individual. Crítico em ISPs com 100k+ faturas: o caminho
    antigo (1 SELECT+UPSERT por fatura, tudo num só @transaction.atomic)
    estourava o time-limit do worker e revertia tudo, deixando a tabela vazia.
    """
    today = timezone.now().date()
    count = 0
    batch: list[FactInvoice] = []

    def _flush(records: list[FactInvoice]) -> int:
        if not records:
            return 0
        FactInvoice.objects.bulk_create(
            records,
            update_conflicts=True,
            unique_fields=["organization", "invoice"],
            update_fields=[
                "issued_date", "due_date", "paid_date", "amount",
                "paid_amount", "late_fee_amount", "status",
                "days_overdue", "aging_bucket",
            ],
            batch_size=_FACT_FINANCIAL_BATCH,
        )
        return len(records)

    invoices = Invoice.objects.filter(organization=organization)
    for inv in invoices.iterator(chunk_size=_FACT_FINANCIAL_BATCH):
        days_overdue, bucket = _aging_bucket(inv.due_date, inv.paid_at, inv.status, today)
        batch.append(
            FactInvoice(
                organization=organization,
                invoice=inv,
                issued_date=inv.issued_at.date() if inv.issued_at else inv.due_date,
                due_date=inv.due_date,
                paid_date=inv.paid_at.date() if inv.paid_at else None,
                amount=inv.amount,
                paid_amount=inv.paid_amount,
                late_fee_amount=_parse_late_fee(inv.raw_extras),
                status=inv.status,
                days_overdue=days_overdue,
                aging_bucket=bucket,
            )
        )
        if len(batch) >= _FACT_FINANCIAL_BATCH:
            count += _flush(batch)
            batch = []

    count += _flush(batch)
    return count


def _rebuild_fact_payment(organization: Organization) -> int:
    """Materializa FactPayment via bulk_create com update_conflicts."""
    count = 0
    batch: list[FactPayment] = []

    def _flush(records: list[FactPayment]) -> int:
        if not records:
            return 0
        FactPayment.objects.bulk_create(
            records,
            update_conflicts=True,
            unique_fields=["organization", "payment"],
            update_fields=["paid_date", "amount", "method"],
            batch_size=_FACT_FINANCIAL_BATCH,
        )
        return len(records)

    payments = Payment.objects.filter(organization=organization)
    for pay in payments.iterator(chunk_size=_FACT_FINANCIAL_BATCH):
        batch.append(
            FactPayment(
                organization=organization,
                payment=pay,
                paid_date=pay.paid_at.date(),
                amount=pay.amount,
                method=pay.method,
            )
        )
        if len(batch) >= _FACT_FINANCIAL_BATCH:
            count += _flush(batch)
            batch = []

    count += _flush(batch)
    return count


def _rebuild_fact_expense(organization: Organization) -> int:
    """Materializa FactExpense via bulk_create com update_conflicts."""
    count = 0
    batch: list[FactExpense] = []

    def _flush(records: list[FactExpense]) -> int:
        if not records:
            return 0
        FactExpense.objects.bulk_create(
            records,
            update_conflicts=True,
            unique_fields=["organization", "expense"],
            update_fields=[
                "expense_date", "due_date", "paid_date", "amount",
                "paid_amount", "status", "category", "supplier_name", "description",
            ],
            batch_size=_FACT_FINANCIAL_BATCH,
        )
        return len(records)

    expenses = Expense.objects.filter(organization=organization)
    for exp in expenses.iterator(chunk_size=_FACT_FINANCIAL_BATCH):
        batch.append(
            FactExpense(
                organization=organization,
                expense=exp,
                # expense_date = paid_at se pago, otherwise due_date
                expense_date=exp.paid_at if exp.paid_at else exp.due_date,
                due_date=exp.due_date,
                paid_date=exp.paid_at,
                amount=exp.amount,
                paid_amount=exp.paid_amount,
                status=exp.status,
                category=exp.category,
                supplier_name=exp.supplier_name,
                description=exp.description,
            )
        )
        if len(batch) >= _FACT_FINANCIAL_BATCH:
            count += _flush(batch)
            batch = []

    count += _flush(batch)
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


@allow_cross_tenant(reason="analytics rebuild itera models de domínio sem context HTTP")
@transaction.atomic
def rebuild_financial_facts(organization: Organization) -> dict[str, int]:
    """Rematerializa as fact tables financeiras (fatura/pagamento/despesa).

    Rede de segurança agendada (Beat): o rebuild normal roda via signal
    `sync_completed`, mas se ele falhar/for interrompido no bootstrap as fact
    ficam vazias (foi o caso de FactInvoice). Este rebuild idempotente recompõe
    o que sustenta os dashboards de inadimplência/aging/DRE.
    """
    return {
        "fact_invoice": _rebuild_fact_invoice(organization),
        "fact_payment": _rebuild_fact_payment(organization),
        "fact_expense": _rebuild_fact_expense(organization),
    }
