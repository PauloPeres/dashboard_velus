"""Testes do split de receita recebida vs não recebida no DRE (#22).

`compute_open_revenue_series` agrega faturas PENDING/OVERDUE por mês de
vencimento; `compute_dre` expõe o split (contratada/recebida/em aberto) no
resumo do mês, no YTD e na série mensal combinada.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_dre,
    compute_open_revenue_series,
)
from apps.analytics.infrastructure.models import FactInvoice
from apps.financial.infrastructure.models import Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_fact_invoice(
    org: Organization,
    *,
    due_date: date,
    status: str,
    amount: Decimal,
    paid_date: date | None = None,
) -> FactInvoice:
    global _seq
    _seq += 1
    set_current_organization(org)
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"inv-{_seq}",
        contract_external_id="",
        amount=amount,
        due_date=due_date,
        status=status,
    )
    return FactInvoice.objects.create(
        organization=org,
        invoice=invoice,
        issued_date=due_date - timedelta(days=30),
        due_date=due_date,
        paid_date=paid_date,
        amount=amount,
        paid_amount=amount if paid_date else None,
        status=status,
    )


@pytest.mark.django_db
class TestComputeOpenRevenueSeries:
    def test_empty_returns_empty(self, organization_a: Organization) -> None:
        assert compute_open_revenue_series(organization_a) == []

    def test_sums_pending_and_overdue_by_due_month(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        this_month = today.replace(day=1)
        _make_fact_invoice(
            organization_a, due_date=this_month, status="PENDING", amount=Decimal("100")
        )
        _make_fact_invoice(
            organization_a, due_date=this_month, status="OVERDUE", amount=Decimal("50")
        )
        set_current_organization(organization_a)

        series = compute_open_revenue_series(organization_a)
        row = next(r for r in series if r["month"] == this_month.strftime("%Y-%m"))
        assert row["amount"] == 150.0
        assert row["count"] == 2

    def test_excludes_paid_and_canceled(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        this_month = today.replace(day=1)
        _make_fact_invoice(
            organization_a,
            due_date=this_month,
            status="PAID",
            amount=Decimal("100"),
            paid_date=today,
        )
        _make_fact_invoice(
            organization_a, due_date=this_month, status="CANCELED", amount=Decimal("80")
        )
        set_current_organization(organization_a)

        assert compute_open_revenue_series(organization_a) == []


@pytest.mark.django_db
class TestDreRevenueSplit:
    def test_current_month_and_ytd_expose_split(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        this_month = today.replace(day=1)
        # Recebida: PAID com paid_date neste mês
        _make_fact_invoice(
            organization_a,
            due_date=this_month,
            status="PAID",
            amount=Decimal("300"),
            paid_date=today,
        )
        # Em aberto: OVERDUE vencida neste mês
        _make_fact_invoice(
            organization_a, due_date=this_month, status="OVERDUE", amount=Decimal("120")
        )
        set_current_organization(organization_a)

        dre = compute_dre(organization_a)
        cur = dre["current_month"]
        assert cur["receita_recebida"] == 300.0
        assert cur["receita_em_aberto"] == 120.0
        # YTD acumula o mesmo (ano corrente)
        assert dre["ytd"]["receita_recebida"] == 300.0
        assert dre["ytd"]["receita_em_aberto"] == 120.0

    def test_revenue_series_merges_received_and_open(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        this_month = today.replace(day=1)
        _make_fact_invoice(
            organization_a,
            due_date=this_month,
            status="PAID",
            amount=Decimal("200"),
            paid_date=today,
        )
        _make_fact_invoice(
            organization_a, due_date=this_month, status="PENDING", amount=Decimal("70")
        )
        set_current_organization(organization_a)

        dre = compute_dre(organization_a)
        key = this_month.strftime("%Y-%m")
        row = next((r for r in dre["revenue_series"] if r["month"] == key), None)
        assert row is not None
        assert row["received"] == 200.0
        assert row["open"] == 70.0
        assert "mrr" in row
