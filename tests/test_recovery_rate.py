"""Testes do compute_recovery_rate — efetividade de recuperação de inadimplência.

Datas são calculadas relativas a `timezone.now().date()` para o teste ser
robusto independente do dia em que roda. A janela da métrica é: coortes de
vencimento dos últimos 12 meses com pelo menos 90 dias de maturação.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_recovery_rate
from apps.analytics.infrastructure.models import FactInvoice
from apps.financial.infrastructure.models import Invoice
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
    """Cria Invoice (FK obrigatória) + FactInvoice com os campos lidos pela métrica."""
    global _seq
    _seq += 1
    invoice = Invoice(
        organization=org,
        source_type="FAKE",
        external_id=f"inv-{_seq}",
        contract_external_id="",
        amount=amount,
        due_date=due_date,
        status=status,
    )
    invoice.save()
    fact = FactInvoice(
        organization=org,
        invoice=invoice,
        issued_date=due_date - timedelta(days=30),
        due_date=due_date,
        paid_date=paid_date,
        amount=amount,
        paid_amount=amount if paid_date else None,
        status=status,
    )
    fact.save()
    return fact


@pytest.mark.django_db
class TestComputeRecoveryRate:
    def test_empty_returns_zeros(self, organization_a: Organization) -> None:
        result = compute_recovery_rate(organization_a)
        assert result["pct"] == 0.0
        assert result["recovered_amount"] == 0.0
        assert result["delinquent_amount"] == 0.0
        assert result["delinquent_count"] == 0
        assert len(result["by_aging"]) == 4
        assert all(b["pct"] == 0.0 for b in result["by_aging"])

    def test_recovered_and_outstanding_buckets(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        due = today - timedelta(days=120)  # maturada (>90d) e dentro da janela 12m

        # Recuperada 5 dias em atraso → bucket 0_30
        _make_fact_invoice(
            organization_a, due_date=due, status="PAID",
            amount=Decimal("100"), paid_date=due + timedelta(days=5),
        )
        # Recuperada 70 dias em atraso → bucket 61_90
        _make_fact_invoice(
            organization_a, due_date=due, status="PAID",
            amount=Decimal("200"), paid_date=due + timedelta(days=70),
        )
        # Em aberto, vencida há 120 dias → bucket OVER_90, só denominador
        _make_fact_invoice(
            organization_a, due_date=due, status="PENDING",
            amount=Decimal("300"),
        )

        result = compute_recovery_rate(organization_a)

        assert result["delinquent_amount"] == 600.0
        assert result["recovered_amount"] == 300.0
        assert result["outstanding_amount"] == 300.0
        assert result["recovered_count"] == 2
        assert result["delinquent_count"] == 3
        assert result["pct"] == 50.0

        by_key = {b["key"]: b for b in result["by_aging"]}
        assert by_key["0_30"]["recovered"] == 100.0
        assert by_key["0_30"]["pct"] == 100.0
        assert by_key["61_90"]["recovered"] == 200.0
        assert by_key["61_90"]["pct"] == 100.0
        assert by_key["OVER_90"]["total"] == 300.0
        assert by_key["OVER_90"]["recovered"] == 0.0
        assert by_key["OVER_90"]["pct"] == 0.0
        assert by_key["31_60"]["total"] == 0.0

    def test_paid_on_time_excluded(self, organization_a: Organization) -> None:
        """Fatura paga em dia (ou adiantada) nunca inadimpliu — fora do denominador."""
        today = timezone.now().date()
        due = today - timedelta(days=120)
        # Paga 10 dias ANTES do vencimento
        _make_fact_invoice(
            organization_a, due_date=due, status="PAID",
            amount=Decimal("500"), paid_date=due - timedelta(days=10),
        )
        # Paga exatamente no vencimento
        _make_fact_invoice(
            organization_a, due_date=due, status="PAID",
            amount=Decimal("400"), paid_date=due,
        )

        result = compute_recovery_rate(organization_a)
        assert result["delinquent_amount"] == 0.0
        assert result["delinquent_count"] == 0
        assert result["pct"] == 0.0

    def test_canceled_excluded(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        due = today - timedelta(days=120)
        _make_fact_invoice(
            organization_a, due_date=due, status="CANCELED", amount=Decimal("999"),
        )
        result = compute_recovery_rate(organization_a)
        assert result["delinquent_count"] == 0
        assert result["delinquent_amount"] == 0.0

    def test_unmatured_cohort_excluded(self, organization_a: Organization) -> None:
        """Faturas com menos de 90 dias de maturação ficam fora da janela."""
        today = timezone.now().date()
        # Venceu há 30 dias — ainda não maturou (< 90d)
        _make_fact_invoice(
            organization_a, due_date=today - timedelta(days=30),
            status="PENDING", amount=Decimal("700"),
        )
        result = compute_recovery_rate(organization_a)
        assert result["delinquent_count"] == 0

    def test_outside_12m_window_excluded(self, organization_a: Organization) -> None:
        """Coortes mais antigas que a janela de 12 meses não entram."""
        today = timezone.now().date()
        # ~500 dias atrás: muito antes do início da janela
        _make_fact_invoice(
            organization_a, due_date=today - timedelta(days=500),
            status="PENDING", amount=Decimal("123"),
        )
        result = compute_recovery_rate(organization_a)
        assert result["delinquent_count"] == 0

    def test_org_isolation(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        today = timezone.now().date()
        due = today - timedelta(days=120)
        _make_fact_invoice(
            organization_a, due_date=due, status="PENDING", amount=Decimal("100"),
        )
        result_b = compute_recovery_rate(organization_b)
        assert result_b["delinquent_count"] == 0
        result_a = compute_recovery_rate(organization_a)
        assert result_a["delinquent_count"] == 1
