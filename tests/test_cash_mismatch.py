"""Testes de `compute_cash_calendar` — descasamento de caixa dia a dia.

Três modos:
- realized (mês passado): só o efetuado — PAID por paid_date.
- planned (mês que vem): só o planejado — a receber (PENDING/OVERDUE) e a pagar
  (Expense OPEN) por due_date.
- hybrid (mês atual): efetuado até hoje + a vencer depois; itens já vencidos e
  não pagos são empurrados para o dia de hoje.

O saldo acumulado parte de zero a cada mês: revela o vale de liquidez intra-mês
mesmo quando o total fecha no azul.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_cash_calendar
from apps.analytics.infrastructure.models import FactExpense, FactInvoice
from apps.financial.infrastructure.models import Expense, Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _prev_month(d: date) -> tuple[int, int]:
    return (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)


def _next_month(d: date) -> tuple[int, int]:
    return (d.year, d.month + 1) if d.month < 12 else (d.year + 1, 1)


def _invoice(
    org: Organization,
    *,
    due_date: date,
    amount: Decimal,
    status: str,
    paid_date: date | None = None,
) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    inv = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cc-inv-{_seq}",
        contract_external_id="",
        amount=amount,
        due_date=due_date,
        status=status,
    )
    FactInvoice.objects.create(
        organization=org,
        invoice=inv,
        issued_date=due_date - timedelta(days=30),
        due_date=due_date,
        paid_date=paid_date,
        amount=amount,
        paid_amount=amount if paid_date else None,
        status=status,
    )


def _expense(
    org: Organization,
    *,
    due_date: date,
    amount: Decimal,
    status: str,
    paid_date: date | None = None,
) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    exp = Expense.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cc-exp-{_seq}",
        amount=amount,
        due_date=due_date,
        paid_at=paid_date,
        status=status,
    )
    FactExpense.objects.create(
        organization=org,
        expense=exp,
        expense_date=paid_date if paid_date else due_date,
        due_date=due_date,
        paid_date=paid_date,
        amount=amount,
        status=status,
    )


@pytest.mark.django_db
class TestCashCalendarRealized:
    def test_realized_uses_paid_date(self, organization_a: Organization) -> None:
        py, pm = _prev_month(timezone.now().date())
        # Recebe no dia 20, paga no dia 5 — ambos efetuados (PAID).
        _invoice(
            organization_a,
            due_date=date(py, pm, 18),
            amount=Decimal("1000"),
            status="PAID",
            paid_date=date(py, pm, 20),
        )
        _expense(
            organization_a,
            due_date=date(py, pm, 5),
            amount=Decimal("400"),
            status="PAID",
            paid_date=date(py, pm, 5),
        )
        # Item em aberto NÃO entra no modo realized.
        _expense(
            organization_a, due_date=date(py, pm, 10), amount=Decimal("999"),
            status="OPEN",
        )
        set_current_organization(organization_a)

        data = compute_cash_calendar(organization_a, py, pm, "realized")
        assert data["inflow"][19] == pytest.approx(1000.0)  # dia 20 → idx 19
        assert data["outflow"][4] == pytest.approx(400.0)  # dia 5 → idx 4
        s = data["summary"]
        assert s["total_in"] == pytest.approx(1000.0)
        assert s["total_out"] == pytest.approx(400.0)  # OPEN ignorado
        assert s["num_days"] == calendar.monthrange(py, pm)[1]
        assert data["today_day"] is None


@pytest.mark.django_db
class TestCashCalendarPlanned:
    def test_planned_uses_due_date_and_open_status(
        self, organization_a: Organization
    ) -> None:
        ny, nm = _next_month(timezone.now().date())
        # A receber (PENDING) dia 15; a pagar (OPEN) dia 8.
        _invoice(
            organization_a, due_date=date(ny, nm, 15), amount=Decimal("2000"),
            status="PENDING",
        )
        _expense(
            organization_a, due_date=date(ny, nm, 8), amount=Decimal("700"),
            status="OPEN",
        )
        # Já pago não entra no planejado.
        _expense(
            organization_a, due_date=date(ny, nm, 8), amount=Decimal("123"),
            status="PAID", paid_date=date(ny, nm, 8),
        )
        set_current_organization(organization_a)

        data = compute_cash_calendar(organization_a, ny, nm, "planned")
        assert data["inflow"][14] == pytest.approx(2000.0)  # dia 15
        assert data["outflow"][7] == pytest.approx(700.0)  # dia 8, só o OPEN
        s = data["summary"]
        assert s["total_out"] == pytest.approx(700.0)
        # Paga dia 8, recebe dia 15 → fica negativo no meio do mês.
        assert s["worst_balance"] == pytest.approx(-700.0)
        assert s["worst_day"] == 8
        assert s["breakeven_day"] == 15
        assert s["net"] == pytest.approx(1300.0)


@pytest.mark.django_db
class TestCashCalendarHybrid:
    def test_hybrid_mixes_realized_and_pending(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        y, m = today.year, today.month
        # Algo já pago neste mês no dia 1 (efetuado).
        _expense(
            organization_a, due_date=date(y, m, 1), amount=Decimal("300"),
            status="PAID", paid_date=date(y, m, 1),
        )
        # Algo a vencer no futuro (último dia do mês) — a pagar.
        last = calendar.monthrange(y, m)[1]
        _expense(
            organization_a, due_date=date(y, m, last), amount=Decimal("500"),
            status="OPEN",
        )
        set_current_organization(organization_a)

        data = compute_cash_calendar(organization_a, y, m, "hybrid")
        assert data["outflow"][0] == pytest.approx(300.0)  # dia 1 efetuado
        assert data["outflow"][last - 1] == pytest.approx(500.0)  # a vencer
        s = data["summary"]
        assert s["total_out"] == pytest.approx(800.0)
        assert data["today_day"] == today.day

    def test_hybrid_clamps_overdue_to_today(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        y, m = today.year, today.month
        if today.day < 3:
            pytest.skip("precisa de pelo menos 2 dias decorridos no mês")
        # Vencido e não pago no dia 1 (antes de hoje) → deve cair no dia de hoje.
        _expense(
            organization_a, due_date=date(y, m, 1), amount=Decimal("600"),
            status="OPEN",
        )
        set_current_organization(organization_a)

        data = compute_cash_calendar(organization_a, y, m, "hybrid")
        assert data["outflow"][0] == 0.0  # nada no dia 1
        assert data["outflow"][today.day - 1] == pytest.approx(600.0)  # hoje


@pytest.mark.django_db
class TestCashCalendarEmpty:
    def test_empty_org_zeroed_structure(self, organization_a: Organization) -> None:
        py, pm = _prev_month(timezone.now().date())
        set_current_organization(organization_a)
        data = compute_cash_calendar(organization_a, py, pm, "realized")
        nd = calendar.monthrange(py, pm)[1]
        assert data["days"] == list(range(1, nd + 1))
        assert len(data["inflow"]) == nd
        assert all(v == 0.0 for v in data["inflow"])
        s = data["summary"]
        assert s["total_in"] == 0.0
        assert s["worst_day"] == 0
        assert s["days_negative"] == 0
        assert s["breakeven_day"] is None
