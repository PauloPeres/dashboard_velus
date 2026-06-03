"""Testes do painel de Compromissos Futuros.

As parcelas de M&A e dívida já estão pré-lançadas no IXC como Expenses OPEN
com `due_date` futura. O agregador soma esses compromissos por mês de
vencimento e por camada gerencial (#72), destacando os estruturais (dívida +
M&A) por fornecedor e em que mês cada frente se encerra.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise

import pytest
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from apps.analytics.application.aggregations import compute_compromissos_futuros
from apps.financial.infrastructure.models import Expense
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

# id_conta → tier (via _CONTA_TO_PLANO/_PLANEJAMENTO fallback), igual ao
# test_dre_managerial_tiers:
_CONTA_OPEX = "115"      # operacional
_CONTA_DIVIDA = "147"    # dívida
_CONTA_INVEST = "10028"  # investimento (M&A)

_seq = 0


def _future(n_months: int, day: int = 15) -> date:
    """Retorna um dia no mês `n_months` à frente do mês atual (n>=1 → futuro)."""
    return timezone.now().date().replace(day=day) + relativedelta(months=n_months)


def _open_expense(
    org: Organization,
    *,
    id_conta: str,
    amount: Decimal,
    due_date: date,
    supplier: str = "Fornecedor X",
    status: str = "OPEN",
    deleted_at: object = None,
) -> Expense:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Expense.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-{_seq}",
        supplier_name=supplier,
        supplier_external_id=f"cfsup-{_seq}",
        amount=amount,
        due_date=due_date,
        status=status,
        deleted_at=deleted_at,
        raw_extras={"id_conta": id_conta},
    )


@pytest.mark.django_db
class TestCompromissosFuturos:
    def test_sums_open_future_by_tier(self, organization_a: Organization) -> None:
        _open_expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("1000"), due_date=_future(1))
        _open_expense(organization_a, id_conta=_CONTA_INVEST, amount=Decimal("500"), due_date=_future(2))
        _open_expense(organization_a, id_conta=_CONTA_OPEX, amount=Decimal("300"), due_date=_future(1))
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        s = data["summary"]
        assert s["divida"] == pytest.approx(1000.0)
        assert s["investimento"] == pytest.approx(500.0)
        assert s["recorrente"] == pytest.approx(300.0)
        assert s["total"] == pytest.approx(1800.0)

    def test_excludes_past_paid_and_soft_deleted(
        self, organization_a: Organization
    ) -> None:
        # Futura OPEN — entra
        _open_expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("100"), due_date=_future(1))
        # Passada (mês anterior) — fora
        _open_expense(
            organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("999"),
            due_date=_future(-1),
        )
        # Futura mas PAID — fora (não é compromisso a vencer)
        _open_expense(
            organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("888"),
            due_date=_future(2), status="PAID",
        )
        # Futura OPEN mas soft-deleted — fora
        _open_expense(
            organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("777"),
            due_date=_future(2), deleted_at=timezone.now(),
        )
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        assert data["summary"]["total"] == pytest.approx(100.0)

    def test_structural_rows_track_installments_and_end(
        self, organization_a: Organization
    ) -> None:
        # Mesmo fornecedor de dívida, 3 parcelas em meses consecutivos.
        for n in (1, 2, 3):
            _open_expense(
                organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("400"),
                due_date=_future(n), supplier="BRADESCO",
            )
        # Uma frente de M&A, parcela única.
        _open_expense(
            organization_a, id_conta=_CONTA_INVEST, amount=Decimal("250"),
            due_date=_future(1), supplier="Power Net",
        )
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        rows = {r["name"]: r for r in data["structural"]}

        assert rows["BRADESCO"]["tier"] == "divida"
        assert rows["BRADESCO"]["parcelas"] == 3
        assert rows["BRADESCO"]["total"] == pytest.approx(1200.0)
        # encerra na 3ª parcela
        assert rows["BRADESCO"]["last_month"] == _future(3).strftime("%Y-%m")

        assert rows["Power Net"]["tier"] == "investimento"
        assert rows["Power Net"]["parcelas"] == 1

        # Ordenado por total desc: BRADESCO (1200) antes de Power Net (250)
        assert data["structural"][0]["name"] == "BRADESCO"

    def test_cumulative_starts_at_total_and_is_non_increasing(
        self, organization_a: Organization
    ) -> None:
        _open_expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("600"), due_date=_future(1))
        _open_expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("400"), due_date=_future(2))
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        cum = data["cumulative"]
        assert cum[0] == pytest.approx(1000.0)  # saldo total a quitar no início
        assert all(a >= b - 1e-6 for a, b in pairwise(cum))  # não cresce
        assert cum[-1] >= 0
