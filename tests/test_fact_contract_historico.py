"""Histórico diário de contratos — issue #122.

Até esta issue, `_rebuild_fact_contract_status_daily` escrevia o status de HOJE
em todos os dias do passado. Um contrato cancelado em julho aparecia cancelado
também em janeiro e, portanto, nunca constava da base de janeiro: a série de
base ativa só crescia, e todo gráfico de evolução em cima dela tinha viés de
sobrevivência.

Estes testes prendem a correção: o passado de um contrato cancelado é o passado
em que ele estava vivo.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_contract_status_trend
from apps.analytics.application.rebuild import rebuild_for_capability
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _contrato(
    org: Organization,
    *,
    status: str = "ACTIVE",
    ativado_dias: int = 200,
    cancelado_dias: int | None = None,
    mrr: str = "100",
) -> Contract:
    global _seq
    _seq += 1
    set_current_organization(org)
    agora = timezone.now()
    return Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"fh-{_seq}",
        customer_external_id=f"fh-cust-{_seq}",
        plan_name="Plano X",
        monthly_amount=Decimal(mrr),
        status=status,
        activated_at=agora - timedelta(days=ativado_dias),
        canceled_at=(
            agora - timedelta(days=cancelado_dias)
            if cancelado_dias is not None
            else None
        ),
    )


def _rebuild(org: Organization) -> None:
    rebuild_for_capability(org, "CONTRACTS")


def _ativos_em(org: Organization, dias_atras: int) -> int:
    d = timezone.now().date() - timedelta(days=dias_atras)
    return FactContractStatusDaily.objects.filter(
        organization=org, date=d, is_active=True
    ).count()


@pytest.mark.django_db
class TestContratoCanceladoTemPassado:
    def test_contrato_cancelado_conta_na_base_antes_do_cancelamento(
        self, organization_a: Organization
    ) -> None:
        """O bug da #122 em uma linha."""
        _contrato(organization_a, status="CANCELED", ativado_dias=200, cancelado_dias=30)
        _rebuild(organization_a)
        assert _ativos_em(organization_a, 90) == 1   # estava vivo há 90 dias
        assert _ativos_em(organization_a, 10) == 0   # já tinha saído há 10

    def test_ultimo_dia_ativo_e_a_vespera_do_cancelamento(
        self, organization_a: Organization
    ) -> None:
        _contrato(organization_a, status="CANCELED", ativado_dias=100, cancelado_dias=30)
        _rebuild(organization_a)
        assert _ativos_em(organization_a, 31) == 1
        assert _ativos_em(organization_a, 30) == 0

    def test_serie_de_base_passa_a_mostrar_saida(
        self, organization_a: Organization
    ) -> None:
        """Antes da #122 a base só crescia — nenhuma saída era representável."""
        _contrato(organization_a, status="ACTIVE", ativado_dias=400)
        _contrato(organization_a, status="CANCELED", ativado_dias=400, cancelado_dias=45)
        _rebuild(organization_a)
        série = compute_contract_status_trend(organization_a, months=6)
        ativos = [p["active"] for p in série]
        assert max(ativos) == 2      # os dois estavam vivos
        assert ativos[-1] == 1       # um saiu
        assert ativos[0] > ativos[-1]

    def test_contrato_vivo_ocupa_todo_o_periodo(
        self, organization_a: Organization
    ) -> None:
        _contrato(organization_a, status="ACTIVE", ativado_dias=120)
        _rebuild(organization_a)
        assert _ativos_em(organization_a, 100) == 1
        assert _ativos_em(organization_a, 1) == 1

    def test_cancelamento_no_futuro_nao_gera_dias_futuros(
        self, organization_a: Organization
    ) -> None:
        """Existe contrato com canceled_at no futuro na base real."""
        _contrato(
            organization_a, status="CANCELED", ativado_dias=100, cancelado_dias=-30
        )
        _rebuild(organization_a)
        hoje = timezone.now().date()
        assert not FactContractStatusDaily.objects.filter(
            organization=organization_a, date__gt=hoje
        ).exists()
        assert _ativos_em(organization_a, 0) == 1  # ainda ativo hoje

    def test_cancelado_antes_de_ativar_nao_gera_linha(
        self, organization_a: Organization
    ) -> None:
        _contrato(organization_a, status="CANCELED", ativado_dias=10, cancelado_dias=20)
        _rebuild(organization_a)
        assert FactContractStatusDaily.objects.filter(
            organization=organization_a
        ).count() == 0

    def test_rebuild_e_idempotente(self, organization_a: Organization) -> None:
        _contrato(organization_a, status="CANCELED", ativado_dias=100, cancelado_dias=30)
        _rebuild(organization_a)
        n1 = FactContractStatusDaily.objects.filter(organization=organization_a).count()
        _rebuild(organization_a)
        n2 = FactContractStatusDaily.objects.filter(organization=organization_a).count()
        assert n1 == n2 > 0


@pytest.mark.django_db
class TestStatusHistorico:
    def test_sem_historico_usa_o_status_atual(
        self, organization_a: Organization
    ) -> None:
        """Contrato vivo e bloqueado hoje: não sabemos quando bloqueou.

        Assumir o status atual é chute, mas é o chute honesto — inventar a data
        da transição seria repetir o erro que a #122 corrigiu.
        """
        _contrato(organization_a, status="BLOCKED", ativado_dias=90)
        _rebuild(organization_a)
        d = timezone.now().date() - timedelta(days=60)
        linha = FactContractStatusDaily.objects.get(organization=organization_a, date=d)
        assert linha.status == "BLOCKED"
        assert linha.is_active is True  # bloqueado ainda é base

    def test_mudanca_registrada_no_historico_e_respeitada(
        self, organization_a: Organization
    ) -> None:
        """Com histórico datado, o dia anterior à mudança mantém o status velho."""
        c = _contrato(organization_a, status="ACTIVE", ativado_dias=90)
        c.status = "BLOCKED"
        c.save()

        # O simple_history carimba tudo "agora"; pra exercitar o caminho do
        # histórico é preciso datar os registros no passado.
        registros = list(c.history.all().order_by("history_date"))
        assert len(registros) == 2
        agora = timezone.now()
        c.history.filter(pk=registros[0].pk).update(
            history_date=agora - timedelta(days=90)
        )
        c.history.filter(pk=registros[1].pk).update(
            history_date=agora - timedelta(days=40)
        )

        _rebuild(organization_a)
        hoje = timezone.now().date()

        def status_em(dias: int) -> str:
            return FactContractStatusDaily.objects.get(
                organization=organization_a, date=hoje - timedelta(days=dias)
            ).status

        assert status_em(60) == "ACTIVE"    # antes da mudança
        assert status_em(30) == "BLOCKED"   # depois
        assert status_em(0) == "BLOCKED"
