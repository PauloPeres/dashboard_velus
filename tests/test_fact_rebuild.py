"""Testes do rebuild de fact financeiras (#21).

Cobre a materialização de FactInvoice via bulk_create (idempotente) e o
entrypoint `rebuild_financial_facts`, garantindo que inadimplência/aging
populam a partir de Invoice — o bug original deixava FactInvoice vazia.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_aging_distribution
from apps.analytics.application.rebuild import (
    rebuild_financial_facts,
    rebuild_for_capability,
)
from apps.analytics.infrastructure.models import FactInvoice
from apps.financial.infrastructure.models import Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_invoice(
    org: Organization,
    *,
    status: str,
    amount: Decimal,
    due_days_ago: int,
    paid: bool = False,
) -> Invoice:
    global _seq
    _seq += 1
    set_current_organization(org)
    now = timezone.now()
    due_date = (now - timedelta(days=due_days_ago)).date()
    return Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"inv-{_seq}",
        contract_external_id="",
        amount=amount,
        paid_amount=amount if paid else None,
        due_date=due_date,
        paid_at=now if paid else None,
        status=status,
    )


@pytest.mark.django_db
class TestRebuildFactInvoice:
    def test_rebuild_populates_fact_invoice(self, organization_a: Organization) -> None:
        _make_invoice(organization_a, status="PENDING", amount=Decimal("100"), due_days_ago=45)
        _make_invoice(organization_a, status="PENDING", amount=Decimal("50"), due_days_ago=-10)
        _make_invoice(
            organization_a, status="PAID", amount=Decimal("80"), due_days_ago=20, paid=True
        )

        summary = rebuild_for_capability(organization_a, "INVOICES")
        assert summary["fact_invoice"] == 3

        set_current_organization(organization_a)
        buckets = dict(
            FactInvoice.objects.filter(organization=organization_a).values_list(
                "aging_bucket", "amount"
            )
        )
        assert FactInvoice.objects.filter(organization=organization_a).count() == 3
        assert set(buckets) == {"31_60", "ON_TIME", "PAID"}

    def test_rebuild_idempotent_and_updates(self, organization_a: Organization) -> None:
        inv = _make_invoice(
            organization_a, status="PENDING", amount=Decimal("100"), due_days_ago=45
        )
        rebuild_for_capability(organization_a, "INVOICES")
        rebuild_for_capability(organization_a, "INVOICES")  # re-run

        set_current_organization(organization_a)
        assert FactInvoice.objects.filter(organization=organization_a).count() == 1

        # Pagar a fatura e reprocessar → fato atualizado (sem duplicar).
        inv.status = "PAID"
        inv.paid_at = timezone.now()
        inv.paid_amount = Decimal("100")
        inv.save()
        rebuild_for_capability(organization_a, "INVOICES")

        fact = FactInvoice.objects.get(organization=organization_a, invoice=inv)
        assert FactInvoice.objects.filter(organization=organization_a).count() == 1
        assert fact.aging_bucket == "PAID"

    def test_aging_distribution_after_rebuild(self, organization_a: Organization) -> None:
        _make_invoice(organization_a, status="PENDING", amount=Decimal("200"), due_days_ago=45)
        _make_invoice(organization_a, status="PENDING", amount=Decimal("300"), due_days_ago=100)
        rebuild_for_capability(organization_a, "INVOICES")

        set_current_organization(organization_a)
        dist = {d["key"]: d["amount"] for d in compute_aging_distribution(organization_a)}
        assert dist["31_60"] == pytest.approx(200.0)
        assert dist["OVER_90"] == pytest.approx(300.0)

    def test_rebuild_financial_facts_entrypoint(self, organization_a: Organization) -> None:
        _make_invoice(organization_a, status="PENDING", amount=Decimal("100"), due_days_ago=10)
        summary = rebuild_financial_facts(organization_a)
        assert summary["fact_invoice"] == 1
        assert summary["fact_payment"] == 0
        assert summary["fact_expense"] == 0

    def test_org_isolation(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        _make_invoice(organization_a, status="PENDING", amount=Decimal("100"), due_days_ago=45)
        _make_invoice(organization_b, status="PENDING", amount=Decimal("100"), due_days_ago=45)
        rebuild_for_capability(organization_a, "INVOICES")

        set_current_organization(organization_a)
        assert FactInvoice.objects.filter(organization=organization_a).count() == 1
        set_current_organization(organization_b)
        assert FactInvoice.objects.filter(organization=organization_b).count() == 0
