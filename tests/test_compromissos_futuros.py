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
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.financial.infrastructure.models import Expense
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

# id_conta → tier (via _CONTA_TO_PLANO/_PLANEJAMENTO fallback), igual ao
# test_dre_managerial_tiers:
_CONTA_OPEX = "115"      # operacional
_CONTA_DIVIDA = "147"    # dívida
_CONTA_INVEST = "10028"  # investimento / M&A (cod 1.2.01)
_CONTA_CAPEX = "47"      # imobilizado / capex (cod 1.2.02.003 Máq. e Equip.)

_seq = 0


def _future(n_months: int, day: int = 15) -> date:
    """Retorna um dia no mês `n_months` à frente do mês atual (n>=1 → futuro)."""
    return timezone.now().date().replace(day=day) + relativedelta(months=n_months)


def _mrr_snapshot(org: Organization, *, monthly: Decimal) -> None:
    """Cria um snapshot ativo de hoje pra alimentar o MRR (faturamento mensal)."""
    global _seq
    _seq += 1
    set_current_organization(org)
    contract = Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-ctr-{_seq}",
        customer_external_id=f"cf-cust-{_seq}",
        plan_name="Plano X",
        monthly_amount=monthly,
        status="ACTIVE",
    )
    FactContractStatusDaily.objects.create(
        organization=org,
        contract=contract,
        date=timezone.now().date(),
        status="ACTIVE",
        monthly_amount=monthly,
        is_active=True,
    )


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

    def test_divida_multiplo_faturamento(self, organization_a: Organization) -> None:
        # Faturamento mensal (MRR) = R$1.000; dívida a vencer = R$3.000.
        _mrr_snapshot(organization_a, monthly=Decimal("1000"))
        _open_expense(
            organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("2000"),
            due_date=_future(1),
        )
        _open_expense(
            organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("1000"),
            due_date=_future(2),
        )
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        s = data["summary"]
        assert s["faturamento_mensal"] == pytest.approx(1000.0)
        # R$3.000 de dívida ÷ R$1.000/mês = 3× faturamento.
        assert s["divida_multiplo_faturamento"] == pytest.approx(3.0)

    def test_multiplo_zero_sem_faturamento(
        self, organization_a: Organization
    ) -> None:
        # Sem snapshot de MRR, não divide por zero — múltiplo vira 0.
        _open_expense(
            organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("500"),
            due_date=_future(1),
        )
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        assert data["summary"]["divida_multiplo_faturamento"] == pytest.approx(0.0)

    def test_capex_separated_from_ma(self, organization_a: Organization) -> None:
        # M&A real (conta 1.2.01) e capex/imobilizado (conta 1.2.02.x) caem na
        # mesma seção DRE, mas devem aparecer em camadas distintas.
        _open_expense(
            organization_a, id_conta=_CONTA_INVEST, amount=Decimal("800"),
            due_date=_future(1), supplier="Power Net",
        )
        _open_expense(
            organization_a, id_conta=_CONTA_CAPEX, amount=Decimal("100"),
            due_date=_future(1), supplier="INTELBRAS S/A",
        )
        set_current_organization(organization_a)

        data = compute_compromissos_futuros(organization_a, months_ahead=12)
        s = data["summary"]
        # M&A não inclui o capex.
        assert s["investimento"] == pytest.approx(800.0)
        assert s["capex"] == pytest.approx(100.0)
        assert s["total"] == pytest.approx(900.0)

        # Capex NÃO entra na tabela de frentes estruturais (M&A + dívida).
        names = {r["name"] for r in data["structural"]}
        assert "Power Net" in names
        assert "INTELBRAS S/A" not in names
        tiers = {r["tier"] for r in data["structural"]}
        assert "capex" not in tiers

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
