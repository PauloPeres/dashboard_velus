"""Histórico diário do score de risco — issue #123.

`ChurnRiskScore` guarda uma linha por cliente, sobrescrita a cada execução.
Isso torna irrespondível a única pergunta que valida o algoritmo: *dos clientes
marcados como HIGH em maio, quantos cancelaram?* — o score de maio não existe
mais.

Estes testes prendem o que a #123 acrescenta: a foto de cada dia, gravada uma
vez, nunca reescrita retroativamente.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.churn_risk import compute_churn_risk_scores
from apps.analytics.infrastructure.models import (
    ChurnRiskScore,
    FactChurnRiskDaily,
)
from apps.customers.infrastructure.models import Contract, Customer
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _cliente_em_risco(org: Organization, *, mrr: str = "100") -> Customer:
    """Cliente com contrato BLOCKED — dispara o sinal de bloqueio prolongado."""
    global _seq
    _seq += 1
    set_current_organization(org)
    cliente = Customer.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"crh-cust-{_seq}",
        document=f"doc-{_seq}",
        name=f"Cliente {_seq}",
        status=Customer.Status.ACTIVE.value,
    )
    Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"crh-ctr-{_seq}",
        customer=cliente,
        customer_external_id=cliente.external_id,
        plan_name="Plano X",
        monthly_amount=Decimal(mrr),
        status="BLOCKED",
        activated_at=timezone.now() - timedelta(days=300),
    )
    return cliente


@pytest.mark.django_db
class TestHistoricoDiario:
    def test_calcular_score_grava_a_foto_do_dia(
        self, organization_a: Organization
    ) -> None:
        _cliente_em_risco(organization_a)
        resumo = compute_churn_risk_scores(organization_a)
        hoje = timezone.now().date()

        assert resumo["at_risk"] == 1
        assert resumo["historico"] == 1
        linha = FactChurnRiskDaily.objects.get(organization=organization_a, date=hoje)
        atual = ChurnRiskScore.objects.get(organization=organization_a)
        assert (linha.score, linha.level) == (atual.score, atual.level)
        assert linha.signals == atual.signals
        assert linha.monthly_amount == atual.monthly_amount

    def test_recomputar_no_mesmo_dia_nao_duplica(
        self, organization_a: Organization
    ) -> None:
        _cliente_em_risco(organization_a)
        compute_churn_risk_scores(organization_a)
        compute_churn_risk_scores(organization_a)
        assert FactChurnRiskDaily.objects.filter(organization=organization_a).count() == 1

    def test_dia_anterior_nao_e_reescrito(
        self, organization_a: Organization
    ) -> None:
        """O oposto do que o FactContractStatusDaily fazia antes da #122."""
        cliente = _cliente_em_risco(organization_a)
        compute_churn_risk_scores(organization_a)
        hoje = timezone.now().date()
        ontem = hoje - timedelta(days=1)

        # Simula a execução de ontem, com outro score.
        set_current_organization(organization_a)
        FactChurnRiskDaily.objects.create(
            organization=organization_a, customer=cliente, date=ontem,
            score=15, level="LOW", signals=[{"code": "OFFLINE"}],
            monthly_amount=Decimal("100"),
        )

        compute_churn_risk_scores(organization_a)

        de_ontem = FactChurnRiskDaily.objects.get(
            organization=organization_a, date=ontem
        )
        assert de_ontem.score == 15
        assert de_ontem.level == "LOW"
        de_hoje = FactChurnRiskDaily.objects.get(organization=organization_a, date=hoje)
        assert de_hoje.score != 15

    def test_cliente_que_sai_do_risco_perde_o_score_mas_nao_o_passado(
        self, organization_a: Organization
    ) -> None:
        """É exatamente o caso que hoje some sem deixar rastro."""
        cliente = _cliente_em_risco(organization_a)
        compute_churn_risk_scores(organization_a)
        hoje = timezone.now().date()
        assert FactChurnRiskDaily.objects.filter(customer=cliente, date=hoje).exists()

        # Cliente regulariza: contrato volta a ACTIVE e o sinal deixa de disparar.
        set_current_organization(organization_a)
        Contract.objects.filter(organization=organization_a).update(status="ACTIVE")
        compute_churn_risk_scores(organization_a)

        assert not ChurnRiskScore.objects.filter(customer=cliente).exists()
        assert FactChurnRiskDaily.objects.filter(customer=cliente, date=hoje).exists()

    def test_ringbuffer_descarta_o_que_passou_da_janela(
        self, organization_a: Organization
    ) -> None:
        cliente = _cliente_em_risco(organization_a)
        set_current_organization(organization_a)
        antigo = timezone.now().date() - timedelta(days=500)
        FactChurnRiskDaily.objects.create(
            organization=organization_a, customer=cliente, date=antigo,
            score=40, level="MEDIUM", signals=[], monthly_amount=Decimal("100"),
        )
        compute_churn_risk_scores(organization_a)
        assert not FactChurnRiskDaily.objects.filter(date=antigo).exists()

    def test_org_sem_risco_nao_grava_nada(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        resumo = compute_churn_risk_scores(organization_a)
        assert resumo["historico"] == 0
        assert not FactChurnRiskDaily.objects.filter(organization=organization_a).exists()
