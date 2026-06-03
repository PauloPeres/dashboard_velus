"""Inadimplência só conta faturas recuperáveis — contrato ACTIVE/BLOCKED (#item1).

`compute_kpis` (delinquência) e `compute_aging_distribution` filtravam faturas
PENDING/OVERDUE sem olhar o contrato dono, inflando a inadimplência em ~15-30x
com faturas órfãs (sem contrato) e de contratos CANCELED. Estes testes fixam o
escopo: só faturas de contratos ainda na base (ACTIVE + BLOCKED) entram.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_aging_distribution,
    compute_kpis,
)
from apps.analytics.infrastructure.models import FactInvoice
from apps.customers.infrastructure.models import Contract
from apps.financial.infrastructure.models import Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_overdue_invoice(
    org: Organization,
    *,
    amount: Decimal,
    contract_status: str | None,
    aging_bucket: str = "OVER_90",
) -> FactInvoice:
    """Cria fatura vencida; contract_status=None => fatura órfã (sem contrato)."""
    global _seq
    _seq += 1
    set_current_organization(org)

    contract = None
    if contract_status is not None:
        contract = Contract.objects.create(
            organization=org,
            source_type="FAKE",
            external_id=f"ctr-{_seq}",
            customer_external_id=f"cust-{_seq}",
            plan_name="Plano X",
            monthly_amount=Decimal("100.00"),
            status=contract_status,
        )

    today = timezone.now().date()
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"inv-{_seq}",
        contract_external_id=contract.external_id if contract else "",
        contract=contract,
        amount=amount,
        due_date=today - timedelta(days=100),
        status="OVERDUE",
    )
    return FactInvoice.objects.create(
        organization=org,
        invoice=invoice,
        issued_date=today - timedelta(days=130),
        due_date=today - timedelta(days=100),
        amount=amount,
        status="OVERDUE",
        days_overdue=100,
        aging_bucket=aging_bucket,
    )


@pytest.mark.django_db
class TestDelinquencyRecoverableScope:
    def _seed_mixed(self, org: Organization) -> None:
        # Recuperáveis: contam (ACTIVE + BLOCKED) = 100 + 50 = 150
        _make_overdue_invoice(org, amount=Decimal("100"), contract_status="ACTIVE")
        _make_overdue_invoice(org, amount=Decimal("50"), contract_status="BLOCKED")
        # Não recuperáveis: NÃO contam
        _make_overdue_invoice(org, amount=Decimal("9999"), contract_status="CANCELED")
        _make_overdue_invoice(org, amount=Decimal("8888"), contract_status=None)

    def test_kpis_delinquency_only_counts_recoverable(
        self, organization_a: Organization
    ) -> None:
        self._seed_mixed(organization_a)
        kpis = compute_kpis(organization_a)
        assert kpis["delinquency_amount"] == 150.0
        assert kpis["delinquency_count"] == 2

    def test_aging_only_counts_recoverable(
        self, organization_a: Organization
    ) -> None:
        self._seed_mixed(organization_a)
        aging = compute_aging_distribution(organization_a)
        over_90 = next(r for r in aging if r["key"] == "OVER_90")
        assert over_90["amount"] == 150.0
        assert over_90["count"] == 2
        # nenhum bucket deve carregar as faturas órfã/cancelada
        assert sum(r["amount"] for r in aging) == 150.0
