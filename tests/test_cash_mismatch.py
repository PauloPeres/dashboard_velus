"""Testes de `compute_cash_mismatch` — descasamento de caixa por dia do mês.

Agrega recebimentos (FactInvoice PAID) e pagamentos (FactExpense PAID) pelo DIA
do mês (1-31), tira a média dos N meses fechados e acumula o saldo intra-mês a
partir de zero. O ponto é revelar QUANDO o caixa entra vs QUANDO sai: mesmo com
o total mensal fechando no azul, a concentração de saídas cedo no mês abre um
buraco de liquidez que os recebimentos só cobrem depois.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_cash_mismatch
from apps.analytics.infrastructure.models import FactExpense, FactInvoice
from apps.financial.infrastructure.models import Expense, Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _closed_month_day(day: int) -> date:
    """Um dia `day` dentro do mês fechado anterior ao atual."""
    first_this_month = timezone.now().date().replace(day=1)
    last_prev_month = first_this_month - timedelta(days=1)
    return last_prev_month.replace(day=day)


def _paid_invoice(org: Organization, *, paid_date: date, amount: Decimal) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cm-inv-{_seq}",
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


def _paid_expense(org: Organization, *, paid_date: date, amount: Decimal) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    exp = Expense.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cm-exp-{_seq}",
        amount=amount,
        due_date=paid_date,
        paid_at=paid_date,
        status="PAID",
    )
    FactExpense.objects.create(
        organization=org,
        expense=exp,
        expense_date=paid_date,
        due_date=paid_date,
        paid_date=paid_date,
        amount=amount,
        status="PAID",
    )


@pytest.mark.django_db
class TestCashMismatch:
    def test_aggregates_by_day_of_month(self, organization_a: Organization) -> None:
        # Recebimento no dia 20, pagamento no dia 5.
        _paid_invoice(
            organization_a, paid_date=_closed_month_day(20), amount=Decimal("1000")
        )
        _paid_expense(
            organization_a, paid_date=_closed_month_day(5), amount=Decimal("400")
        )
        set_current_organization(organization_a)

        data = compute_cash_mismatch(organization_a, months=6)
        # inflow_d[idx] onde idx = dia-1 (days começa em 1).
        assert data["inflow"][19] == pytest.approx(1000.0 / 6)
        assert data["outflow"][4] == pytest.approx(400.0 / 6)
        assert data["inflow"][4] == 0.0
        assert data["outflow"][19] == 0.0

    def test_cumulative_balance_dips_then_recovers(
        self, organization_a: Organization
    ) -> None:
        # Paga cedo (dia 3), recebe tarde (dia 25): saldo fica negativo no meio
        # do mês e só vira positivo quando o recebimento entra.
        _paid_expense(
            organization_a, paid_date=_closed_month_day(3), amount=Decimal("600")
        )
        _paid_invoice(
            organization_a, paid_date=_closed_month_day(25), amount=Decimal("1000")
        )
        set_current_organization(organization_a)

        data = compute_cash_mismatch(organization_a, months=1)
        s = data["summary"]
        # No dia 3 já saiu 600 e nada entrou → buraco.
        assert data["cumulative"][2] == pytest.approx(-600.0)
        assert s["worst_balance"] == pytest.approx(-600.0)
        assert s["worst_day"] == 3
        # Dias negativos: do dia 3 ao dia 24 (recebe no 25). 22 dias no vermelho.
        assert s["days_negative"] == 22
        assert s["breakeven_day"] == 25
        # Fecha no azul: 1000 entrou, 600 saiu.
        assert s["net"] == pytest.approx(400.0)
        assert data["cumulative"][-1] == pytest.approx(400.0)

    def test_weighted_average_days_and_mismatch(
        self, organization_a: Organization
    ) -> None:
        # Recebe metade no dia 10 e metade no dia 20 → dia médio ponderado 15.
        _paid_invoice(
            organization_a, paid_date=_closed_month_day(10), amount=Decimal("500")
        )
        _paid_invoice(
            organization_a, paid_date=_closed_month_day(20), amount=Decimal("500")
        )
        # Paga tudo no dia 5 → dia médio de pagamento 5.
        _paid_expense(
            organization_a, paid_date=_closed_month_day(5), amount=Decimal("300")
        )
        set_current_organization(organization_a)

        data = compute_cash_mismatch(organization_a, months=3)
        s = data["summary"]
        assert s["avg_day_in"] == pytest.approx(15.0)
        assert s["avg_day_out"] == pytest.approx(5.0)
        # Descasamento = recebe (15) menos paga (5) = +10 dias (entra depois).
        assert s["descasamento_dias"] == pytest.approx(10.0)

    def test_empty_org_returns_zeroed_structure(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        data = compute_cash_mismatch(organization_a, months=6)
        assert data["days"] == list(range(1, 32))
        assert len(data["inflow"]) == 31
        assert len(data["outflow"]) == 31
        assert all(v == 0.0 for v in data["inflow"])
        assert all(v == 0.0 for v in data["cumulative"])
        s = data["summary"]
        assert s["total_in"] == 0.0
        assert s["total_out"] == 0.0
        assert s["avg_day_in"] == 0.0
        assert s["worst_day"] == 0
        assert s["days_negative"] == 0
        assert s["breakeven_day"] is None
