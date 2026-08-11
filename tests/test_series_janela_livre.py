"""Séries com janela livre — Fase 2 do épico #100.

O risco desta fase não é a feature nova: é **quebrar o que já funcionava**. As
mesmas funções alimentam páginas que continuam mensais por decisão (DRE, MRR,
Burn, Forecast), então cada uma ganhou um modo novo em vez de trocar de modo.

Por isso metade deste arquivo é regressão: o modo legado `months=N` tem que
continuar entregando exatamente o que entregava. A outra metade cobre o modo
novo `start`/`end`.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application import time_buckets as tb
from apps.analytics.application.aggregations import (
    compute_cash_received_series,
    compute_contract_status_trend,
    compute_delinquency_trend,
)
from apps.analytics.infrastructure.models import (
    FactContractStatusDaily,
    FactInvoice,
)
from apps.customers.infrastructure.models import Contract
from apps.financial.infrastructure.models import Invoice
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _invoice(
    org: Organization,
    *,
    amount: str,
    due: date,
    status: str = "PENDING",
    days_overdue: int = 10,
    paid_date: date | None = None,
    late_fee: str = "0",
) -> FactInvoice:
    global _seq
    _seq += 1
    set_current_organization(org)
    invoice = Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"sjl-inv-{_seq}",
        contract_external_id="",
        amount=Decimal(amount),
        due_date=due,
        status=status,
    )
    return FactInvoice.objects.create(
        organization=org,
        invoice=invoice,
        issued_date=due - timedelta(days=30),
        due_date=due,
        paid_date=paid_date,
        amount=Decimal(amount),
        late_fee_amount=Decimal(late_fee),
        paid_amount=Decimal(amount) if paid_date else None,
        status=status,
        days_overdue=days_overdue,
    )


def _snapshot(org: Organization, *, d: date, status: str, quantos: int) -> None:
    global _seq
    set_current_organization(org)
    for _ in range(quantos):
        _seq += 1
        contract = Contract.objects.create(
            organization=org,
            source_type="FAKE",
            external_id=f"sjl-ctr-{_seq}",
            customer_external_id=f"sjl-cust-{_seq}",
            plan_name="Plano X",
            monthly_amount=Decimal("100"),
            status=status,
        )
        FactContractStatusDaily.objects.create(
            organization=org,
            contract=contract,
            date=d,
            status=status,
            is_active=True,
            monthly_amount=Decimal("100"),
        )


def _hoje() -> date:
    return timezone.now().date()


# =============================================================================
# Regressão — o modo legado não pode ter mudado
# =============================================================================
@pytest.mark.django_db
class TestModoLegadoIntacto:
    """`months=N` continua sendo o que era: uma linha por mês COM dado.

    Sem preenchimento de buracos, sem chave nova obrigatória, sem reordenação.
    Forecast, DRE, MRR e Burn dependem disso.
    """

    def test_caixa_recebido_so_lista_meses_com_dado(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        _invoice(
            organization_a, amount="100", due=hoje - timedelta(days=40),
            status="PAID", paid_date=hoje - timedelta(days=40),
        )
        série = compute_cash_received_series(organization_a, months=12)
        # Um mês só: o do pagamento. Nada de 12 pontos com zeros.
        assert len(série) == 1
        assert set(série[0]) == {"month", "label", "amount", "count"}
        assert série[0]["amount"] == 100.0

    def test_inadimplencia_so_lista_meses_com_dado(
        self, organization_a: Organization
    ) -> None:
        _invoice(organization_a, amount="80", due=_hoje() - timedelta(days=20))
        série = compute_delinquency_trend(organization_a, months=12)
        assert len(série) == 1
        assert set(série[0]) == {
            "month", "label", "principal", "late_fee", "amount", "count",
        }

    def test_contratos_por_status_rende_um_ponto_por_mes_pedido(
        self, organization_a: Organization
    ) -> None:
        _snapshot(organization_a, d=_hoje(), status="ACTIVE", quantos=3)
        série = compute_contract_status_trend(organization_a, months=6)
        assert len(série) == 6  # eixo fixo de N meses, como sempre foi
        assert série[-1]["active"] == 3
        assert set(série[0]) == {
            "month", "label", "active", "blocked", "awaiting", "total",
        }


# =============================================================================
# Modo novo — janela livre
# =============================================================================
@pytest.mark.django_db
class TestJanelaLivre:
    def test_caixa_por_dia_preenche_o_eixo_inteiro(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        _invoice(
            organization_a, amount="50", due=hoje - timedelta(days=2),
            status="PAID", paid_date=hoje - timedelta(days=2),
        )
        série = compute_cash_received_series(
            organization_a, start=hoje - timedelta(days=6), end=hoje
        )
        # 7 dias de janela → 7 pontos, mesmo com dado em um só.
        assert len(série) == 7
        assert sum(p["amount"] for p in série) == 50.0
        assert [p["amount"] for p in série].count(0.0) == 6

    def test_janela_curta_vira_dia_e_longa_vira_mes(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        curta = compute_cash_received_series(
            organization_a, start=hoje - timedelta(days=6), end=hoje
        )
        longa = compute_cash_received_series(
            organization_a, start=hoje - timedelta(days=364), end=hoje
        )
        assert len(curta) == 7            # dia a dia
        assert 12 <= len(longa) <= 13     # mês a mês

    def test_granularidade_pode_ser_forcada(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        série = compute_cash_received_series(
            organization_a,
            start=hoje - timedelta(days=6),
            end=hoje,
            granularity=tb.MONTH,
        )
        assert len(série) <= 2  # 7 dias podem cruzar a virada de mês

    def test_inadimplencia_respeita_os_dois_extremos(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        _invoice(organization_a, amount="10", due=hoje - timedelta(days=1))
        _invoice(organization_a, amount="20", due=hoje - timedelta(days=10))
        série = compute_delinquency_trend(
            organization_a, start=hoje - timedelta(days=2), end=hoje
        )
        assert sum(p["amount"] for p in série) == 10.0  # o de 10 dias ficou fora

    def test_inadimplencia_nao_conta_vencimento_futuro(
        self, organization_a: Organization
    ) -> None:
        """Fatura que ainda vai vencer não é inadimplência."""
        hoje = _hoje()
        _invoice(organization_a, amount="99", due=hoje + timedelta(days=5))
        série = compute_delinquency_trend(
            organization_a, start=hoje - timedelta(days=3), end=hoje + timedelta(days=10)
        )
        assert sum(p["amount"] for p in série) == 0.0

    def test_contratos_por_bucket_e_snapshot_e_nao_soma(
        self, organization_a: Organization
    ) -> None:
        """Contrato ativo é estoque: dois dias com 3 ativos não viram 6."""
        hoje = _hoje()
        _snapshot(organization_a, d=hoje - timedelta(days=1), status="ACTIVE", quantos=3)
        _snapshot(organization_a, d=hoje, status="ACTIVE", quantos=3)
        série = compute_contract_status_trend(
            organization_a,
            start=hoje - timedelta(days=1),
            end=hoje,
            granularity=tb.WEEK,
        )
        assert len(série) <= 2
        assert max(p["active"] for p in série) == 3

    def test_contratos_usa_o_ultimo_dia_com_dado_do_bucket(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        _snapshot(organization_a, d=hoje - timedelta(days=3), status="ACTIVE", quantos=2)
        _snapshot(organization_a, d=hoje - timedelta(days=1), status="ACTIVE", quantos=5)
        série = compute_contract_status_trend(
            organization_a,
            start=hoje - timedelta(days=6),
            end=hoje,
            granularity=tb.WEEK,
        )
        # Um bucket semanal cobrindo os dois snapshots → vale o mais recente.
        com_dado = [p for p in série if p["total"]]
        assert com_dado[-1]["active"] == 5

    def test_bucket_sem_snapshot_vira_zero_e_nao_some(
        self, organization_a: Organization
    ) -> None:
        hoje = _hoje()
        _snapshot(organization_a, d=hoje, status="ACTIVE", quantos=1)
        série = compute_contract_status_trend(
            organization_a, start=hoje - timedelta(days=4), end=hoje
        )
        assert len(série) == 5
        assert [p["total"] for p in série].count(0) == 4


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPaginaFinanceiro:
    URL = "/financial/"

    def test_barra_tem_presets_em_dias(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(self.URL)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "setPeriodo('1d')" in html
        assert "setPeriodo('ontem')" in html
        assert 'name="de"' in html
        assert resp.context["period"].key == "30d"
        assert resp.context["period_warning"] is None

    def test_titulo_avisa_a_granularidade_da_serie(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        assert client.get(f"{self.URL}?periodo=7d").context[
            "serie_granularidade"
        ] == "dia a dia"
        assert client.get(f"{self.URL}?periodo=3m").context[
            "serie_granularidade"
        ] == "semana a semana"
        assert client.get(f"{self.URL}?periodo=12m").context[
            "serie_granularidade"
        ] == "mês a mês"

    def test_kpis_de_estoque_continuam_marcados(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        """A Fase 3 não pode ter sido desfeita pela Fase 2."""
        client.force_login(user_a)
        body = client.get(self.URL).content.decode()
        assert body.count("posição de agora") == 7
        assert body.count("janela fixa") == 3


@pytest.mark.django_db
class TestNetAdds:
    """Net adds é FLUXO: o bucket soma eventos, ao contrário do estoque."""

    def _contrato(
        self, org: Organization, *, ativado: Any = None, cancelado: Any = None
    ) -> None:
        global _seq
        _seq += 1
        set_current_organization(org)
        Contract.objects.create(
            organization=org,
            source_type="FAKE",
            external_id=f"na-ctr-{_seq}",
            customer_external_id=f"na-cust-{_seq}",
            plan_name="Plano X",
            monthly_amount=Decimal("100"),
            status="CANCELED" if cancelado else "ACTIVE",
            activated_at=ativado,
            canceled_at=cancelado,
        )

    def test_modo_legado_rende_um_ponto_por_mes_pedido(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_net_adds_series

        série = compute_net_adds_series(organization_a, months=6)
        assert len(série) == 6
        assert set(série[0]) == {"month", "label", "adds", "churn", "net"}

    def test_janela_livre_soma_eventos_do_bucket(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_net_adds_series

        agora = timezone.now()
        ontem = agora - timedelta(days=1)
        self._contrato(organization_a, ativado=ontem)
        self._contrato(organization_a, ativado=ontem)
        self._contrato(organization_a, cancelado=ontem)

        hoje = _hoje()
        série = compute_net_adds_series(
            organization_a, start=hoje - timedelta(days=6), end=hoje
        )
        assert len(série) == 7
        do_dia = next(p for p in série if p["adds"] or p["churn"])
        assert (do_dia["adds"], do_dia["churn"], do_dia["net"]) == (2, 1, 1)

    def test_janela_livre_corta_nos_dois_extremos(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_net_adds_series

        agora = timezone.now()
        self._contrato(organization_a, ativado=agora - timedelta(days=10))
        hoje = _hoje()
        série = compute_net_adds_series(
            organization_a, start=hoje - timedelta(days=2), end=hoje
        )
        assert sum(p["adds"] for p in série) == 0


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPaginaVendas:
    URL = "/sales/"

    def test_barra_tem_presets_em_dias(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(self.URL)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "setPeriodo('1d')" in html
        assert 'name="de"' in html
        assert resp.context["period"].key == "30d"
        assert resp.context["period_warning"] is None

    def test_serie_muda_de_granularidade_com_a_janela(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        assert client.get(f"{self.URL}?periodo=7d").context[
            "serie_granularidade"
        ] == "dia a dia"
        assert client.get(f"{self.URL}?periodo=12m").context[
            "serie_granularidade"
        ] == "mês a mês"

    def test_blocos_fora_do_filtro_continuam_marcados(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        """A Fase 2 não pode ter desfeito a Fase 3."""
        client.force_login(user_a)
        assert client.get(self.URL).content.decode().count("posição de agora") == 6


@pytest.mark.django_db
class TestFluxoDeCaixa:
    """Receita e despesa no mesmo eixo — o zip strict é a garantia disso."""

    def _despesa(self, org: Organization, *, valor: str, quando: date) -> None:
        global _seq
        _seq += 1
        set_current_organization(org)
        from apps.analytics.infrastructure.models import FactExpense
        from apps.financial.infrastructure.models import Expense

        despesa = Expense.objects.create(
            organization=org,
            source_type="FAKE",
            external_id=f"fc-exp-{_seq}",
            amount=Decimal(valor),
            due_date=quando,
            paid_at=quando,
            status="PAID",
            supplier_name="Fornecedor Teste",
        )
        FactExpense.objects.create(
            organization=org,
            expense=despesa,
            expense_date=quando,
            due_date=quando,
            amount=Decimal(valor),
            status="PAID",
        )

    def test_modo_legado_intacto(self, organization_a: Organization) -> None:
        from apps.analytics.application.aggregations import compute_expense_series

        self._despesa(organization_a, valor="70", quando=_hoje() - timedelta(days=40))
        série = compute_expense_series(organization_a, months=12)
        assert len(série) == 1
        assert set(série[0]) == {"month", "label", "expenses", "count"}

    def test_receita_e_despesa_ficam_no_mesmo_eixo(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_cashflow_series

        hoje = _hoje()
        # Receita num dia, despesa em OUTRO: o eixo precisa conter os dois, cada
        # um no seu bucket — e não colapsar num ponto só.
        _invoice(
            organization_a, amount="300", due=hoje - timedelta(days=5),
            status="PAID", paid_date=hoje - timedelta(days=5),
        )
        self._despesa(organization_a, valor="100", quando=hoje - timedelta(days=2))

        série = compute_cashflow_series(
            organization_a, start=hoje - timedelta(days=6), end=hoje
        )
        assert len(série) == 7
        assert sum(p["revenue"] for p in série) == 300.0
        assert sum(p["expenses"] for p in série) == 100.0
        # Nenhum bucket tem receita e despesa juntas — elas caíram em dias
        # diferentes e o eixo respeitou isso.
        assert not [p for p in série if p["revenue"] and p["expenses"]]
        assert série[-1]["cumulative_net"] == 200.0

    def test_acumulado_e_monotonico_no_eixo(
        self, organization_a: Organization
    ) -> None:
        from apps.analytics.application.aggregations import compute_cashflow_series

        hoje = _hoje()
        _invoice(
            organization_a, amount="10", due=hoje - timedelta(days=3),
            status="PAID", paid_date=hoje - timedelta(days=3),
        )
        _invoice(
            organization_a, amount="20", due=hoje - timedelta(days=1),
            status="PAID", paid_date=hoje - timedelta(days=1),
        )
        série = compute_cashflow_series(
            organization_a, start=hoje - timedelta(days=4), end=hoje
        )
        acumulados = [p["cumulative_net"] for p in série]
        assert acumulados == sorted(acumulados)
        assert acumulados[-1] == 30.0


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPaginaFluxoDeCaixa:
    URL = "/financial/cashflow/"

    def test_barra_tem_presets_em_dias(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(self.URL)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "setPeriodo('1d')" in html
        assert 'name="de"' in html
        assert resp.context["period"].key == "30d"
        assert resp.context["period_warning"] is None

    def test_kpi_do_ultimo_bucket_deixa_de_dizer_mes(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        """Com janela em dias, "Receita (último mês)" era mentira."""
        client.force_login(user_a)
        body = client.get(f"{self.URL}?periodo=7d").content.decode()
        assert "último mês" not in body
        assert "Receita (" in body

    def test_serie_muda_de_granularidade_com_a_janela(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        assert client.get(f"{self.URL}?periodo=7d").context[
            "serie_granularidade"
        ] == "dia a dia"
        assert client.get(f"{self.URL}?periodo=12m").context[
            "serie_granularidade"
        ] == "mês a mês"
