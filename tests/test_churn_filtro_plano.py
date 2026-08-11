"""Filtro por plano na página de Churn — issue #117.

O ponto do filtro é recortar a página INTEIRA por plano. E o que faz esse
recorte ser honesto é ele filtrar os **dois lados** da taxa: os cancelados e a
base ativa que serve de denominador. Sem isso, `logo_churn_pct` viraria
"contribuição do plano pro churn total" com cara de taxa de churn.

A tabela de churn por plano fica de fora do filtro de propósito — é o navegador
por onde se escolhe o plano.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_churn_by_reason,
    compute_churn_summary,
    compute_ltv_distribution,
    compute_mrr_churn_series,
)
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

URL = "/churn/"
_seq = 0


def _contrato(
    org: Organization,
    *,
    plano: str,
    mrr: str = "100",
    status: str = "ACTIVE",
    ativado_dias: int = 400,
    cancelado_dias: int | None = None,
    motivo: str | None = None,
) -> Contract:
    global _seq
    _seq += 1
    set_current_organization(org)
    agora = timezone.now()
    return Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-{_seq}",
        customer_external_id=f"cf-cust-{_seq}",
        plan_name=plano,
        monthly_amount=Decimal(mrr),
        status=status,
        activated_at=agora - timedelta(days=ativado_dias),
        canceled_at=(
            agora - timedelta(days=cancelado_dias)
            if cancelado_dias is not None
            else None
        ),
        raw_extras={"motivo_cancelamento": motivo} if motivo else {},
    )


def _base_ativa(org: Organization, contrato: Contract, *, quando: Any) -> None:
    set_current_organization(org)
    FactContractStatusDaily.objects.create(
        organization=org,
        contract=contrato,
        date=quando,
        status="ACTIVE",
        is_active=True,
        monthly_amount=contrato.monthly_amount,
    )


@pytest.fixture
def base(organization_a: Organization) -> dict[str, Any]:
    """Dois planos, com churn deste mês em cada um."""
    hoje = timezone.now().date()
    mes_passado = (hoje.replace(day=1) - timedelta(days=1)).replace(day=1)

    premium_cancelado = _contrato(
        organization_a, plano="Premium", mrr="300",
        status="CANCELED", cancelado_dias=1, motivo="1",
    )
    basico_cancelado = _contrato(
        organization_a, plano="Básico", mrr="80",
        status="CANCELED", cancelado_dias=1, motivo="2",
    )
    # Base ativa no início do mês: 2 de cada plano (denominador da taxa).
    for plano in ("Premium", "Básico"):
        for _ in range(2):
            ativo = _contrato(organization_a, plano=plano)
            _base_ativa(organization_a, ativo, quando=mes_passado)
    return {
        "premium_cancelado": premium_cancelado,
        "basico_cancelado": basico_cancelado,
    }


@pytest.mark.django_db
class TestAgregacoesComPlano:
    def test_sem_filtro_soma_os_dois_planos(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        resumo = compute_churn_summary(organization_a)
        assert resumo["logo_churn_this_month"] == 2
        assert resumo["mrr_lost_this_month"] == 380.0

    def test_filtro_recorta_o_numerador(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        resumo = compute_churn_summary(organization_a, plano="Premium")
        assert resumo["logo_churn_this_month"] == 1
        assert resumo["mrr_lost_this_month"] == 300.0

    def test_filtro_recorta_tambem_o_denominador(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        """O ponto do filtro: a taxa continua sendo uma taxa.

        1 cancelado ÷ 2 ativos do plano = 50%. Se a base não fosse recortada,
        daria 1 ÷ 4 = 25% — que não é o churn do Premium, é a contribuição dele.
        """
        assert compute_churn_summary(organization_a, plano="Premium")[
            "logo_churn_pct"
        ] == 50.0

    def test_serie_mensal_segue_o_plano(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        série = compute_mrr_churn_series(organization_a, months=3, plano="Básico")
        assert sum(p["mrr_lost"] for p in série) == 80.0

    def test_motivos_seguem_o_plano(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        motivos = compute_churn_by_reason(organization_a, months=3, plano="Premium")
        assert sum(m["count"] for m in motivos) == 1

    def test_ltv_segue_o_plano(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        dist = compute_ltv_distribution(organization_a, plano="Premium")
        assert sum(b["count"] for b in dist) == 1

    def test_plano_inexistente_nao_derruba(
        self, organization_a: Organization, base: dict[str, Any]
    ) -> None:
        resumo = compute_churn_summary(organization_a, plano="Não Existe")
        assert resumo["logo_churn_this_month"] == 0
        assert resumo["logo_churn_pct"] == 0.0


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPaginaChurn:
    def test_seletor_lista_os_planos_da_org(
        self, client: Any, user_a: User, base: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        assert resp.context["planos"] == ["Básico", "Premium"]
        assert resp.context["plano_selecionado"] is None

    def test_filtro_aplica_e_aparece_na_faixa(
        self, client: Any, user_a: User, base: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?plano=Premium")
        assert resp.context["plano_selecionado"] == "Premium"
        assert resp.context["summary"]["logo_churn_this_month"] == 1
        assert "Plano:" in resp.content.decode()

    def test_plano_invalido_e_ignorado_em_silencio(
        self, client: Any, user_a: User, base: dict[str, Any]
    ) -> None:
        """Querystring torta não filtra nada e não quebra a página."""
        client.force_login(user_a)
        resp = client.get(f"{URL}?plano=%3Cscript%3E")
        assert resp.status_code == 200
        assert resp.context["plano_selecionado"] is None
        assert resp.context["summary"]["logo_churn_this_month"] == 2

    def test_tabela_por_plano_nao_e_filtrada(
        self, client: Any, user_a: User, base: dict[str, Any]
    ) -> None:
        """Ela é o navegador do filtro — filtrar colapsaria a comparação.

        `compute_churn_plan_detail` nem recebe `plano`, então isso é estrutural;
        o que este teste prende é o **aviso na tela**, pra ninguém ler a tabela
        achando que ela seguiu o recorte da página.
        """
        client.force_login(user_a)
        resp = client.get(f"{URL}?plano=Premium")
        assert resp.status_code == 200
        assert "todos os planos" in resp.content.decode()

    def test_plano_entra_nos_params_extras_do_periodo(
        self, client: Any, user_a: User, base: dict[str, Any]
    ) -> None:
        """O recorte da página acompanha a escolha de período.

        Aqui não há form de período personalizado (a página é de granularidade
        mensal, ver #100), então o hidden input não existe — mas o recorte fica
        registrado no contexto e o `setPeriodo` do base.html preserva a
        querystring ao trocar de preset.
        """
        client.force_login(user_a)
        resp = client.get(f"{URL}?plano=Premium")
        assert resp.context["period_extra_params"] == {"plano": "Premium"}

    def test_selos_da_fase_3_continuam_no_lugar(
        self, client: Any, user_a: User, base: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        body = client.get(reverse("dashboards:churn")).content.decode()
        assert body.count("posição de agora") == 6
