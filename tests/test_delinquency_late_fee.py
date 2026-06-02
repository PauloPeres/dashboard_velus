"""Testes da separação de inadimplência: principal (MRR) vs multa/juros (#41).

Cobre o parser puro `_parse_late_fee` (multa + juros do raw_extras do IXC, com
fallback gracioso) e a integração rebuild → `compute_delinquency_trend`, que
expõe `principal`, `late_fee` e `amount` (total) por mês de vencimento.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_delinquency_trend
from apps.analytics.application.rebuild import _parse_late_fee, rebuild_for_capability
from apps.analytics.infrastructure.models import FactInvoice
from apps.financial.infrastructure.models import Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


class TestParseLateFee:
    def test_sums_multa_and_juros(self) -> None:
        assert _parse_late_fee({"valor_multas": "10.00", "valor_juros": "5.50"}) == Decimal("15.50")

    def test_handles_comma_decimal(self) -> None:
        assert _parse_late_fee({"valor_multas": "10,25"}) == Decimal("10.25")

    def test_missing_keys_fall_back_to_zero(self) -> None:
        assert _parse_late_fee({"valor_aberto": "79.90"}) == Decimal("0")

    def test_zero_and_blank_are_zero(self) -> None:
        assert _parse_late_fee({"valor_multas": "0", "valor_juros": ""}) == Decimal("0")

    def test_non_dict_is_zero(self) -> None:
        assert _parse_late_fee(None) == Decimal("0")
        assert _parse_late_fee("garbage") == Decimal("0")

    def test_invalid_value_is_zero(self) -> None:
        assert _parse_late_fee({"valor_multas": "abc", "valor_juros": "3.00"}) == Decimal("3.00")


_seq = 0


def _make_invoice(
    org: Organization,
    *,
    amount: Decimal,
    due_days_ago: int,
    raw_extras: dict[str, Any] | None = None,
) -> Invoice:
    global _seq
    _seq += 1
    set_current_organization(org)
    now = timezone.now()
    return Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"inv-fee-{_seq}",
        contract_external_id="",
        amount=amount,
        due_date=(now - timedelta(days=due_days_ago)).date(),
        status="PENDING",
        raw_extras=raw_extras or {},
    )


@pytest.mark.django_db
class TestDelinquencySplit:
    def test_rebuild_populates_late_fee_amount(self, organization_a: Organization) -> None:
        _make_invoice(
            organization_a,
            amount=Decimal("100"),
            due_days_ago=45,
            raw_extras={"valor_multas": "8.00", "valor_juros": "2.00"},
        )
        rebuild_for_capability(organization_a, "INVOICES")
        set_current_organization(organization_a)
        fact = FactInvoice.objects.get(organization=organization_a)
        assert fact.amount == Decimal("100")
        assert fact.late_fee_amount == Decimal("10.00")

    def test_trend_separates_principal_and_late_fee(self, organization_a: Organization) -> None:
        # duas faturas vencidas no mesmo mês: uma com multa, outra sem
        _make_invoice(
            organization_a,
            amount=Decimal("100"),
            due_days_ago=40,
            raw_extras={"valor_multas": "10.00", "valor_juros": "5.00"},
        )
        _make_invoice(organization_a, amount=Decimal("50"), due_days_ago=42)
        rebuild_for_capability(organization_a, "INVOICES")

        trend = compute_delinquency_trend(organization_a, months=6)
        total = next(r for r in trend if r["count"] == 2)
        assert total["principal"] == 150.0
        assert total["late_fee"] == 15.0
        assert total["amount"] == 165.0

    def test_trend_graceful_without_fees(self, organization_a: Organization) -> None:
        _make_invoice(organization_a, amount=Decimal("70"), due_days_ago=35)
        rebuild_for_capability(organization_a, "INVOICES")

        trend = compute_delinquency_trend(organization_a, months=6)
        row = next(r for r in trend if r["count"] == 1)
        assert row["principal"] == 70.0
        assert row["late_fee"] == 0.0
        assert row["amount"] == 70.0
