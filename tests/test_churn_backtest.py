"""Harness de backtest dos sinais de risco — issue #125.

O que estes testes garantem não é o número: é que o número **quer dizer** o que
o relatório diz que quer dizer. Um harness que erra a conta é pior que nenhum,
porque dá autoridade de dado a uma opinião.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.analytics.application.churn_backtest import formatar, rodar_backtest
from apps.analytics.infrastructure.models import FactChurnRiskDaily
from apps.customers.infrastructure.models import Contract, Customer
from apps.helpdesk.infrastructure.models import Ticket
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _cliente(org: Organization) -> Customer:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Customer.objects.create(
        organization=org, source_type="FAKE", external_id=f"bt-cust-{_seq}",
        document=f"bt-doc-{_seq}", name=f"Cliente {_seq}",
        status=Customer.Status.ACTIVE.value,
    )


def _contrato(
    org: Organization, cliente: Customer, *, ativado_dias: int,
    cancelado_dias: int | None = None,
) -> Contract:
    global _seq
    _seq += 1
    agora = timezone.now()
    return Contract.objects.create(
        organization=org, source_type="FAKE", external_id=f"bt-ctr-{_seq}",
        customer=cliente, customer_external_id=cliente.external_id,
        plan_name="Plano X", monthly_amount=Decimal("100"),
        status="CANCELED" if cancelado_dias is not None else "ACTIVE",
        activated_at=agora - timedelta(days=ativado_dias),
        canceled_at=(
            agora - timedelta(days=cancelado_dias)
            if cancelado_dias is not None else None
        ),
    )


def _chamados(org: Organization, cliente: Customer, *, quantos: int, dias: int) -> None:
    global _seq
    for _ in range(quantos):
        _seq += 1
        Ticket.objects.create(
            organization=org, source_type="FAKE", external_id=f"bt-tk-{_seq}",
            customer_external_id=cliente.external_id, subject_id="1",
            technician_id="", status="CLOSED", priority="NORMAL",
            protocol=f"P-{_seq}", opened_at=timezone.now() - timedelta(days=dias),
        )


@pytest.mark.django_db
class TestBase:
    def test_base_exclui_quem_ja_tinha_cancelado_em_d0(
        self, organization_a: Organization
    ) -> None:
        d0 = timezone.now().date() - timedelta(days=120)
        c1 = _cliente(organization_a)
        _contrato(organization_a, c1, ativado_dias=400, cancelado_dias=200)  # antes
        c2 = _cliente(organization_a)
        _contrato(organization_a, c2, ativado_dias=400)                      # vivo

        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        assert bt.base == 1

    def test_base_exclui_quem_ativou_depois_de_d0(
        self, organization_a: Organization
    ) -> None:
        d0 = timezone.now().date() - timedelta(days=120)
        c = _cliente(organization_a)
        _contrato(organization_a, c, ativado_dias=30)
        assert rodar_backtest(organization_a, d0=d0, horizonte=120).base == 0

    def test_desfecho_so_conta_dentro_do_horizonte(
        self, organization_a: Organization
    ) -> None:
        d0 = timezone.now().date() - timedelta(days=120)
        dentro = _cliente(organization_a)
        _contrato(organization_a, dentro, ativado_dias=400, cancelado_dias=60)
        fora = _cliente(organization_a)
        _contrato(organization_a, fora, ativado_dias=400, cancelado_dias=1)

        bt = rodar_backtest(organization_a, d0=d0, horizonte=30)
        assert bt.base == 2
        assert bt.cancelados == 0  # os dois cancelaram depois de D0+30

        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        assert bt.cancelados == 2


@pytest.mark.django_db
class TestLift:
    def test_lift_acima_de_1_quando_o_sinal_separa(
        self, organization_a: Organization
    ) -> None:
        """20 com sinal (todos cancelam) e 20 sem (nenhum cancela)."""
        d0 = timezone.now().date() - timedelta(days=120)
        for _ in range(20):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400, cancelado_dias=60)
            _chamados(organization_a, c, quantos=3, dias=130)
        for _ in range(20):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400)

        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        sinal = next(s for s in bt.sinais if s.nome.startswith("FREQUENT_TICKETS"))
        assert sinal.n == 20
        assert sinal.churn_no_grupo == 100.0
        assert sinal.churn_fora == 0.0
        assert sinal.separa is True
        assert sinal.cobertura == 100.0

    def test_lift_abaixo_de_1_quando_o_sinal_aponta_pro_lado_errado(
        self, organization_a: Organization
    ) -> None:
        """O caso da #124: quem tem o sinal cancela MENOS."""
        d0 = timezone.now().date() - timedelta(days=120)
        for _ in range(20):  # com sinal, ninguém cancela
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400)
            _chamados(organization_a, c, quantos=3, dias=130)
        for _ in range(20):  # sem sinal, todos cancelam
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400, cancelado_dias=60)

        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        sinal = next(s for s in bt.sinais if s.nome.startswith("FREQUENT_TICKETS"))
        assert sinal.lift == 0.0
        assert sinal.separa is False
        assert "aponta pro lado errado" in formatar(bt)

    def test_amostra_pequena_e_descartada(
        self, organization_a: Organization
    ) -> None:
        """Lift de n=3 é anedota com cara de métrica — foi assim que a #124 quase deu errado."""
        d0 = timezone.now().date() - timedelta(days=120)
        for _ in range(3):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400, cancelado_dias=60)
            _chamados(organization_a, c, quantos=3, dias=130)
        for _ in range(20):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400)

        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        assert not [s for s in bt.sinais if s.nome.startswith("FREQUENT_TICKETS")]


@pytest.mark.django_db
class TestScoreHistorico:
    def test_avalia_o_score_que_o_modelo_deu_no_dia(
        self, organization_a: Organization
    ) -> None:
        """A validação sem reconstrução — só possível a partir da #123."""
        d0 = timezone.now().date() - timedelta(days=120)
        marcados = []
        for _ in range(20):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400, cancelado_dias=60)
            marcados.append(c)
        for _ in range(20):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400)

        set_current_organization(organization_a)
        for c in marcados:
            FactChurnRiskDaily.objects.create(
                organization=organization_a, customer=c, date=d0,
                score=60, level="HIGH", signals=[], monthly_amount=Decimal("100"),
            )

        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        alto = next(s for s in bt.sinais if s.nome == "score do dia: HIGH")
        assert alto.n == 20
        assert alto.churn_no_grupo == 100.0
        assert alto.separa is True

    def test_sem_historico_no_dia_nao_reporta_score(
        self, organization_a: Organization
    ) -> None:
        d0 = timezone.now().date() - timedelta(days=120)
        for _ in range(20):
            c = _cliente(organization_a)
            _contrato(organization_a, c, ativado_dias=400)
        bt = rodar_backtest(organization_a, d0=d0, horizonte=120)
        assert not [s for s in bt.sinais if s.nome.startswith("score do dia")]


@pytest.mark.django_db
class TestComando:
    def test_roda_e_imprime(self, organization_a: Organization) -> None:
        out = StringIO()
        call_command("churn_backtest", organization_a.slug, stdout=out)
        texto = out.getvalue()
        assert "D0 =" in texto
        assert "base em D0" in texto

    def test_org_inexistente(self) -> None:
        with pytest.raises(CommandError, match="não encontrada"):
            call_command("churn_backtest", "nao-existe")

    def test_d0_no_futuro_e_recusado(self, organization_a: Organization) -> None:
        futuro = (timezone.now().date() + timedelta(days=5)).isoformat()
        with pytest.raises(CommandError, match="passado"):
            call_command("churn_backtest", organization_a.slug, f"--d0={futuro}")

    def test_d0_invalido(self, organization_a: Organization) -> None:
        with pytest.raises(CommandError, match="YYYY-MM-DD"):
            call_command("churn_backtest", organization_a.slug, "--d0=13/04/2026")
