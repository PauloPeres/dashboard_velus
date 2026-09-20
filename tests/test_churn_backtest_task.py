"""A task semanal que grava o placar do churn.

O backtest existia desde a #125, mas só por linha de comando: rodava, imprimia
no terminal e o número morria ali. A pergunta "o modelo está acertando?" não
pode depender de alguém lembrar de rodar um comando — e a série de execuções é
o que diz se ele está melhorando, piorando ou parado.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.infrastructure.models import ChurnBacktestRun
from apps.analytics.tasks import run_churn_backtest_for_all_orgs
from apps.customers.infrastructure.models import Contract, Customer
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


def _contrato(
    org: Organization, *, external_id: str, ativado_dias: int, cancelado_dias: int | None
) -> Contract:
    set_current_organization(org)
    agora = timezone.now()
    cliente = Customer.objects.create(
        organization=org, source_type="IXC", external_id=f"cli-{external_id}",
        name=f"Cliente {external_id}",
    )
    return Contract.objects.create(
        organization=org,
        source_type="IXC",
        external_id=external_id,
        customer=cliente,
        customer_external_id=cliente.external_id,
        plan_name="Fibra 500MB",
        monthly_amount=Decimal("99.90"),
        status=Contract.Status.CANCELED if cancelado_dias else Contract.Status.ACTIVE,
        activated_at=agora - timedelta(days=ativado_dias),
        canceled_at=agora - timedelta(days=cancelado_dias) if cancelado_dias else None,
    )


@pytest.mark.django_db
class TestTaskDeBacktest:
    def test_grava_o_resultado_para_a_tela_ler(
        self, organization_a: Organization
    ) -> None:
        for i in range(12):
            _contrato(organization_a, external_id=f"c{i}", ativado_dias=400, cancelado_dias=None)
        _contrato(organization_a, external_id="cancelado", ativado_dias=400, cancelado_dias=30)

        resultado = run_churn_backtest_for_all_orgs(horizonte=90)

        assert resultado["com_amostra"] == 1
        run = ChurnBacktestRun.objects.get(organization=organization_a)
        assert run.horizon_days == 90
        assert run.base_size == 13
        assert run.canceled == 1

    def test_d0_respeita_a_janela_de_desfecho(
        self, organization_a: Organization
    ) -> None:
        """D0 é hoje menos o horizonte. Avaliar data mais recente mediria um
        desfecho que ainda não teve tempo de acontecer, e o resultado sairia
        artificialmente bom — todo mundo "ainda não cancelou"."""
        _contrato(organization_a, external_id="c1", ativado_dias=400, cancelado_dias=None)
        run_churn_backtest_for_all_orgs(horizonte=90)
        run = ChurnBacktestRun.objects.get(organization=organization_a)
        esperado = (timezone.now() - timedelta(days=90)).date()
        assert run.d0 == esperado

    def test_rerodar_o_mesmo_d0_atualiza_em_vez_de_duplicar(
        self, organization_a: Organization
    ) -> None:
        """O resultado é determinístico: duas linhas iguais só confundiriam a
        série."""
        _contrato(organization_a, external_id="c1", ativado_dias=400, cancelado_dias=None)
        run_churn_backtest_for_all_orgs(horizonte=90)
        run_churn_backtest_for_all_orgs(horizonte=90)
        assert ChurnBacktestRun.objects.filter(organization=organization_a).count() == 1

    def test_org_sem_base_nao_grava_linha_vazia(
        self, organization_a: Organization
    ) -> None:
        """Linha com base zero não é resultado, é ruído na série."""
        resultado = run_churn_backtest_for_all_orgs(horizonte=90)
        assert resultado["com_amostra"] == 0
        assert ChurnBacktestRun.objects.count() == 0
