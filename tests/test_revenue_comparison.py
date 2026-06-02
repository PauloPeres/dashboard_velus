"""Testes dos cards comparativos mês atual × mês anterior em /revenue/ (#27).

`compute_revenue_comparison` agrega MRR, ARPU, net adds e receita recebida para
o mês corrente e o anterior, expondo variação absoluta e percentual.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _pct_delta,
    compute_revenue_comparison,
)
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.financial.infrastructure.models import Invoice
from apps.analytics.infrastructure.models import FactInvoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _snapshot(org: Organization, *, on: date, monthly: Decimal) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    contract = Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cmp-ctr-{_seq}",
        customer_external_id=f"cmp-cust-{_seq}",
        plan_name="Plano X",
        monthly_amount=monthly,
        status="ACTIVE",
    )
    FactContractStatusDaily.objects.create(
        organization=org,
        contract=contract,
        date=on,
        status="ACTIVE",
        monthly_amount=monthly,
        is_active=True,
    )


def _aware(d: date | None) -> datetime | None:
    return timezone.make_aware(datetime.combine(d, time(12, 0))) if d else None


def _contract_lifecycle(
    org: Organization,
    *,
    activated_at: date | None = None,
    canceled_at: date | None = None,
) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cmp-life-{_seq}",
        customer_external_id=f"cmp-lcust-{_seq}",
        plan_name="Plano X",
        monthly_amount=Decimal("100"),
        status="ACTIVE" if canceled_at is None else "CANCELED",
        activated_at=_aware(activated_at),
        canceled_at=_aware(canceled_at),
    )


def _paid_invoice(org: Organization, *, paid_date: date, amount: Decimal) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cmp-inv-{_seq}",
        contract_external_id="",
        amount=amount,
        due_date=paid_date,
        status="PAID",
    )
    FactInvoice.objects.create(
        organization=org,
        invoice=invoice,
        issued_date=paid_date - timedelta(days=30),
        due_date=paid_date,
        paid_date=paid_date,
        amount=amount,
        paid_amount=amount,
        status="PAID",
    )


class TestPctDelta:
    def test_basic(self) -> None:
        assert _pct_delta(150.0, 100.0) == pytest.approx(50.0)

    def test_zero_base(self) -> None:
        assert _pct_delta(100.0, 0.0) == 0.0

    def test_negative(self) -> None:
        assert _pct_delta(80.0, 100.0) == pytest.approx(-20.0)


@pytest.mark.django_db
class TestComputeRevenueComparison:
    def test_returns_four_metrics_with_keys(
        self, organization_a: Organization
    ) -> None:
        result = compute_revenue_comparison(organization_a)
        keys = {m["key"] for m in result}
        assert keys == {"mrr", "arpu", "net_adds", "received"}
        for m in result:
            assert {"current", "previous", "delta_abs", "delta_pct", "fmt"} <= set(m)

    def test_mrr_and_arpu_current_vs_previous(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        prev_last = today.replace(day=1) - timedelta(days=1)
        # Mês atual: 2 contratos somando 300 (ARPU 150)
        _snapshot(organization_a, on=today, monthly=Decimal("200"))
        _snapshot(organization_a, on=today, monthly=Decimal("100"))
        # Mês anterior: 1 contrato de 100 (ARPU 100)
        _snapshot(organization_a, on=prev_last, monthly=Decimal("100"))
        set_current_organization(organization_a)

        result = {m["key"]: m for m in compute_revenue_comparison(organization_a)}
        assert result["mrr"]["current"] == 300.0
        assert result["mrr"]["previous"] == 100.0
        assert result["mrr"]["delta_abs"] == 200.0
        assert result["arpu"]["current"] == 150.0
        assert result["arpu"]["previous"] == 100.0

    def test_net_adds_splits_by_month(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        month_first = today.replace(day=1)
        prev_first = (month_first - timedelta(days=1)).replace(day=1)
        # Mês atual: 2 novos, 1 cancelado → net +1
        _contract_lifecycle(organization_a, activated_at=month_first)
        _contract_lifecycle(organization_a, activated_at=today)
        _contract_lifecycle(organization_a, canceled_at=today)
        # Mês anterior: 1 novo → net +1
        _contract_lifecycle(organization_a, activated_at=prev_first)
        set_current_organization(organization_a)

        result = {m["key"]: m for m in compute_revenue_comparison(organization_a)}
        assert result["net_adds"]["current"] == 1
        assert result["net_adds"]["previous"] == 1

    def test_received_uses_paid_month(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        month_first = today.replace(day=1)
        prev_mid = (month_first - timedelta(days=1)).replace(day=10)
        _paid_invoice(organization_a, paid_date=today, amount=Decimal("500"))
        _paid_invoice(organization_a, paid_date=prev_mid, amount=Decimal("300"))
        set_current_organization(organization_a)

        result = {m["key"]: m for m in compute_revenue_comparison(organization_a)}
        assert result["received"]["current"] == 500.0
        assert result["received"]["previous"] == 300.0
        assert result["received"]["delta_abs"] == 200.0
