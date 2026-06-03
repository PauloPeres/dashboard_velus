"""Testes da DRE gerencial em camadas (#72).

Cobre as quatro mudanças do #72:
1. EBITDA = resultado operacional (receita − custos − opex), NÃO o líquido.
2. Impostos (Simples Nacional etc.) classificados ABAIXO do EBITDA.
3. Camadas expostas: operacional → impostos → dívida → investimento → caixa.
4. Linha de receita = MRR + avulsas faturadas (cobranças sem contrato).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_avulsas_billed_series,
    compute_dre,
    compute_dre_by_account,
    compute_operational_expense_series,
)
from apps.analytics.infrastructure.models import FactInvoice
from apps.customers.infrastructure.models import Contract
from apps.financial.infrastructure.models import Expense, Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _expense(
    org: Organization,
    *,
    id_conta: str,
    amount: Decimal,
    paid_at: date,
    supplier: str = "Fornecedor X",
) -> None:
    """Cria uma despesa PAGA com id_conta (planejamento_analitico) em raw_extras."""
    global _seq
    _seq += 1
    set_current_organization(org)
    Expense.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"exp-{_seq}",
        supplier_name=supplier,
        supplier_external_id=f"sup-{_seq}",
        amount=amount,
        paid_amount=amount,
        due_date=paid_at,
        paid_at=paid_at,
        status="PAID",
        raw_extras={"id_conta": id_conta},
    )


def _avulsa(
    org: Organization,
    *,
    issued: date,
    amount: Decimal,
    status: str = "PENDING",
) -> None:
    """Cria uma fatura avulsa (sem contrato) materializada em FactInvoice."""
    global _seq
    _seq += 1
    set_current_organization(org)
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"avu-{_seq}",
        contract_external_id="0",  # sentinela de avulsa do IXC
        contract=None,
        amount=amount,
        due_date=issued + timedelta(days=10),
        status=status,
    )
    FactInvoice.objects.create(
        organization=org,
        invoice=invoice,
        issued_date=issued,
        due_date=issued + timedelta(days=10),
        amount=amount,
        status=status,
    )


# id_conta → tier esperado (via _CONTA_TO_PLANO/_PLANEJAMENTO fallback):
#   "3291" → plan 59 (4.1.02) Custos dos Serviços  → operacional
#   "115"  → plan 9  (5.2.x)  Despesas Operacionais → operacional
#   "214"  → plan 64 (4.2.01.003 override) Impostos → impostos
#   "147"  → plan 10 (5.3.x)  Despesas Financeiras  → dívida
#   "10028"→ plan 27 (1.2.01) Investimentos          → investimento
_CONTA_CUSTO = "3291"
_CONTA_OPEX = "115"
_CONTA_IMPOSTO = "214"
_CONTA_DIVIDA = "147"
_CONTA_INVEST = "10028"


@pytest.mark.django_db
class TestAvulsasBilledSeries:
    def test_sums_orphan_invoices_by_issued_month(
        self, organization_a: Organization
    ) -> None:
        this_month = timezone.now().date().replace(day=1)
        _avulsa(organization_a, issued=this_month, amount=Decimal("300"))
        _avulsa(organization_a, issued=this_month, amount=Decimal("200"))
        set_current_organization(organization_a)

        series = compute_avulsas_billed_series(organization_a)
        row = next(r for r in series if r["month"] == this_month.strftime("%Y-%m"))
        assert row["amount"] == 500.0
        assert row["count"] == 2

    def test_excludes_canceled(self, organization_a: Organization) -> None:
        this_month = timezone.now().date().replace(day=1)
        _avulsa(
            organization_a, issued=this_month, amount=Decimal("90"), status="CANCELED"
        )
        set_current_organization(organization_a)
        assert compute_avulsas_billed_series(organization_a) == []


@pytest.mark.django_db
class TestOperationalExpenseSeries:
    def test_only_operational_tier_counted(
        self, organization_a: Organization
    ) -> None:
        m = timezone.now().date().replace(day=1)
        _expense(organization_a, id_conta=_CONTA_CUSTO, amount=Decimal("100"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_OPEX, amount=Decimal("50"), paid_at=m)
        # Não operacionais: NÃO devem entrar
        _expense(organization_a, id_conta=_CONTA_IMPOSTO, amount=Decimal("70"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("40"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_INVEST, amount=Decimal("999"), paid_at=m)
        set_current_organization(organization_a)

        series = compute_operational_expense_series(organization_a)
        row = next(r for r in series if r["month"] == m.strftime("%Y-%m"))
        # 100 (custo) + 50 (opex) = 150; impostos/dívida/invest excluídos
        assert row["expenses"] == 150.0


@pytest.mark.django_db
class TestComputeDreOperationalEbitda:
    def test_ebitda_excludes_non_operational(
        self, organization_a: Organization
    ) -> None:
        m = timezone.now().date().replace(day=1)
        _expense(organization_a, id_conta=_CONTA_OPEX, amount=Decimal("100"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_IMPOSTO, amount=Decimal("60"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("30"), paid_at=m)
        set_current_organization(organization_a)

        dre = compute_dre(organization_a)
        cur = dre["current_month"]
        # despesas do EBITDA = só operacional (100), não 190
        assert cur["despesas"] == 100.0
        # EBITDA = receita − 100 (não − 190). Sem MRR/avulsas, receita=0.
        assert cur["ebitda"] == cur["receita_bruta"] - 100.0

    def test_revenue_includes_avulsas(self, organization_a: Organization) -> None:
        m = timezone.now().date().replace(day=1)
        _avulsa(organization_a, issued=m, amount=Decimal("250"))
        set_current_organization(organization_a)

        dre = compute_dre(organization_a)
        cur = dre["current_month"]
        # sem contratos não há MRR; receita vem só das avulsas
        assert cur["receita_avulsas"] == 250.0
        assert cur["receita_bruta"] == cur["receita_mrr"] + 250.0


@pytest.mark.django_db
class TestComputeDreByAccountTiers:
    def test_tier_subtotals_and_operational_ebitda(
        self, organization_a: Organization
    ) -> None:
        m = timezone.now().date().replace(day=1)
        _expense(organization_a, id_conta=_CONTA_CUSTO, amount=Decimal("100"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_OPEX, amount=Decimal("50"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_IMPOSTO, amount=Decimal("40"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_DIVIDA, amount=Decimal("30"), paid_at=m)
        _expense(organization_a, id_conta=_CONTA_INVEST, amount=Decimal("20"), paid_at=m)
        _avulsa(organization_a, issued=m, amount=Decimal("500"))
        set_current_organization(organization_a)

        data = compute_dre_by_account(organization_a, months=3)
        labels = [r["label"] for r in data["dre_rows"]]
        assert "Resultado Bruto" in labels
        assert "EBITDA Operacional" in labels
        assert "Resultado após Impostos" in labels
        assert "Resultado após Serviço da Dívida" in labels
        assert "Resultado após Investimentos (M&A/Capex)" in labels
        assert "Resultado Líquido (Caixa)" in labels

        summary = data["summary"]
        # EBITDA operacional = receita(avulsas 500) − custos(100) − opex(50) = 350
        assert summary["ebitda"] == pytest.approx(350.0)
        assert summary["impostos"] == pytest.approx(40.0)
        assert summary["divida"] == pytest.approx(30.0)
        assert summary["investimento"] == pytest.approx(20.0)
        # Resultado líquido = 350 − 40 − 30 − 20 = 260
        assert summary["resultado_liquido"] == pytest.approx(260.0)
        # total_expenses soma TODAS as seções
        assert summary["total_expenses"] == pytest.approx(240.0)
        # receita inclui avulsas
        assert summary["total_revenue"] == pytest.approx(500.0)

    def test_tiers_skipped_when_absent(
        self, organization_a: Organization
    ) -> None:
        m = timezone.now().date().replace(day=1)
        # só operacional, nada abaixo do EBITDA
        _expense(organization_a, id_conta=_CONTA_OPEX, amount=Decimal("80"), paid_at=m)
        set_current_organization(organization_a)

        data = compute_dre_by_account(organization_a, months=3)
        labels = [r["label"] for r in data["dre_rows"]]
        assert "EBITDA Operacional" in labels
        # sem impostos/dívida/investimento → subtotais correspondentes ausentes
        assert "Resultado após Impostos" not in labels
        assert "Resultado após Serviço da Dívida" not in labels
        assert "Resultado Líquido (Caixa)" in labels
        assert data["summary"]["impostos"] == 0.0
