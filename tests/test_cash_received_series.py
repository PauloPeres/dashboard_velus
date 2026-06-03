"""Testes de `compute_cash_received_series` — entrada de caixa real por mês.

Fonte única é FactInvoice (status PAID, paid_amount). FactPayment NÃO entra:
ela vem das baixas (fn_areceber_baixas) e tem ~3,5x mais linhas que faturas,
o que inflava o caixa para ~o dobro/triplo do real no /executive/.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_cash_received_series
from apps.analytics.infrastructure.models import FactInvoice, FactPayment
from apps.financial.infrastructure.models import Invoice, Payment
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _paid_invoice(org: Organization, *, paid_date: date, amount: Decimal) -> Invoice:
    global _seq
    _seq += 1
    set_current_organization(org)
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"crs-inv-{_seq}",
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
    return invoice


def _payment_baixa(org: Organization, *, paid_date: date, amount: Decimal) -> None:
    """Cria uma baixa (FactPayment) — ruído que NÃO deve entrar no caixa recebido."""
    global _seq
    _seq += 1
    set_current_organization(org)
    payment = Payment.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"crs-pay-{_seq}",
        amount=amount,
        paid_at=timezone.make_aware(datetime.combine(paid_date, time(12, 0))),
    )
    FactPayment.objects.create(
        organization=org,
        payment=payment,
        paid_date=paid_date,
        amount=amount,
        method="PIX",
    )


@pytest.mark.django_db
class TestCashReceivedSeries:
    def test_uses_factinvoice_paid_amount(
        self, organization_a: Organization
    ) -> None:
        m = timezone.now().date().replace(day=10)
        _paid_invoice(organization_a, paid_date=m, amount=Decimal("1000"))
        _paid_invoice(organization_a, paid_date=m, amount=Decimal("500"))
        set_current_organization(organization_a)

        series = compute_cash_received_series(organization_a, months=12)
        row = next(r for r in series if r["month"] == m.strftime("%Y-%m"))
        assert row["amount"] == pytest.approx(1500.0)
        assert row["count"] == 2

    def test_not_inflated_by_factpayment_baixas(
        self, organization_a: Organization
    ) -> None:
        # Uma fatura paga de R$1.000 → caixa real do mês.
        m = timezone.now().date().replace(day=10)
        _paid_invoice(organization_a, paid_date=m, amount=Decimal("1000"))
        # Mesma fatura gera várias baixas no IXC (fn_areceber_baixas): 4 linhas
        # somando R$1.000 — se entrassem, dobrariam/triplicariam o caixa.
        for _ in range(4):
            _payment_baixa(organization_a, paid_date=m, amount=Decimal("250"))
        set_current_organization(organization_a)

        series = compute_cash_received_series(organization_a, months=12)
        row = next(r for r in series if r["month"] == m.strftime("%Y-%m"))
        # Continua R$1.000 — as baixas não somam por cima.
        assert row["amount"] == pytest.approx(1000.0)

    def test_factpayment_only_month_is_ignored(
        self, organization_a: Organization
    ) -> None:
        # Mês sem fatura, só com baixas (ex.: carnê pré-lançado) → não aparece.
        future = timezone.now().date().replace(day=10) + timedelta(days=400)
        _payment_baixa(organization_a, paid_date=future, amount=Decimal("637"))
        set_current_organization(organization_a)

        series = compute_cash_received_series(organization_a, months=12)
        assert all(r["month"] != future.strftime("%Y-%m") for r in series)
