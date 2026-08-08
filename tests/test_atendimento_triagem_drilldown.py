"""Testes do período em dias + drill-down da página de Atendimento (#89).

A tela de triagem (`/operations/atendimento/`) saiu dos presets mensais e passou
a usar o componente de período (#86) na granularidade em dias, e os gráficos
"Volume por departamento" e "Motivos mais frequentes" viraram drill-downs pra a
lista genérica (#87).

O que importa aqui:

- **paridade de contagem**: o total da lista aberta pelo clique tem que bater
  EXATAMENTE com a altura da barra — por isso os testes comparam com o próprio
  agregador do gráfico (`compute_atendimento_triagem`), nunca com número fixo;
- regra de granularidade da série temporal (dia/semana/mês) derivada da janela;
- compatibilidade da URL antiga `?months=N`;
- RBAC: quem só tem a aba **Atendimento** consegue abrir o drill-down (#87
  deixou a lista gated só por `atendimento_tendencias`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_atendimento_lista,
    compute_atendimento_triagem,
    triagem_trend_granularity,
)
from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.dashboards.period import get_period
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import (
    AccessGroup,
    Organization,
    OrganizationMembership,
    User,
)

_SP = ZoneInfo("America/Sao_Paulo")
URL = "/operations/atendimento/"
LISTA_URL = "/operations/atendimento-lista/"


def _hoje(hour: int = 10) -> datetime:
    """Hora cheia de HOJE (local) — cai em qualquer preset, inclusive "Hoje"."""
    return timezone.localtime(timezone.now(), _SP).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _departamento(org: Organization, *, external_id: str, nome: str) -> Departamento:
    return Departamento.objects.create(
        organization=org, source_type=SourceType.OPA.value,
        external_id=external_id, nome=nome,
    )


def _at(
    org: Organization,
    *,
    external_id: str,
    opened_at: datetime,
    departamento: Departamento | None = None,
    motivos: list[str] | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(hours=1),
        departamento=departamento,
        motivos=motivos or [],
        tags=[],
    )


def _membro_do_grupo(org: Organization, *, email: str, pages: list[str]) -> User:
    """Usuário MEMBER num grupo de acesso com as abas indicadas (RBAC #65)."""
    grupo = AccessGroup.objects.create(
        organization=org, name=f"Grupo {email}", allowed_pages=pages
    )
    user = User.objects.create_user(email=email)
    OrganizationMembership.objects.create(
        user=user, organization=org,
        role=OrganizationMembership.Role.MEMBER, is_active=True,
        access_group=grupo,
    )
    return user


@pytest.fixture
def triagem_seed(organization_a: Organization) -> dict[str, Any]:
    """Dois departamentos e motivos repetidos, dentro e fora do dia de hoje."""
    set_current_organization(organization_a)
    hoje = _hoje()
    sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
    com = _departamento(organization_a, external_id="dcom", nome="Comercial")

    # Suporte hoje: 3 atendimentos, 2 deles com o motivo "Lentidão".
    _at(organization_a, external_id="s1", opened_at=hoje,
        departamento=sup, motivos=["Lentidão", "Sem Cobertura"])
    _at(organization_a, external_id="s2", opened_at=hoje + timedelta(minutes=30),
        departamento=sup, motivos=["Lentidão"])
    _at(organization_a, external_id="s3", opened_at=hoje + timedelta(hours=2),
        departamento=sup, motivos=[])
    # Comercial hoje: 1, com motivo duplicado no MESMO registro (o `has_key` da
    # lista conta 1; o contador do gráfico tem que contar 1 também).
    _at(organization_a, external_id="c1", opened_at=hoje + timedelta(minutes=10),
        departamento=com, motivos=["Upgrade", "Upgrade"])
    # Ruído: mesmo departamento/motivo, mas 10 dias atrás (fora do preset "Hoje").
    _at(organization_a, external_id="velho", opened_at=hoje - timedelta(days=10),
        departamento=sup, motivos=["Lentidão"])
    # Sem departamento (barra que NÃO é clicável — não há id pra filtrar).
    _at(organization_a, external_id="orfao", opened_at=hoje, departamento=None)
    return {"org": organization_a, "sup": sup, "com": com, "hoje": hoje}


# =============================================================================
# Granularidade da série temporal
# =============================================================================
class TestGranularidadeDaSerie:
    """Regra (#89): <= 31 dias → dia; <= 120 → semana; acima → mês."""

    @pytest.mark.parametrize(
        ("dias", "esperado"),
        [(1, "day"), (7, "day"), (31, "day"), (32, "week"), (120, "week"),
         (121, "month"), (365, "month")],
    )
    def test_regra_por_tamanho_da_janela(self, dias: int, esperado: str) -> None:
        end = timezone.localtime(timezone.now(), _SP)
        start = end - timedelta(days=dias - 1)
        assert triagem_trend_granularity(start, end) == esperado


@pytest.mark.django_db
class TestSerieTemporal:
    def test_preset_hoje_rende_um_ponto_por_dia(
        self, triagem_seed: dict[str, Any]
    ) -> None:
        org, hoje = triagem_seed["org"], triagem_seed["hoje"]
        start = hoje.replace(hour=0, minute=0)
        end = start + timedelta(days=1, microseconds=-1)
        data = compute_atendimento_triagem(org, start=start, end=end)
        assert data["trend_granularity"] == "day"
        assert len(data["trend"]) == 1
        # O ruído de 10 dias atrás não entra na janela de hoje.
        assert data["trend"][0]["count"] == data["total"] == 5

    def test_janela_longa_agrega_por_mes(
        self, triagem_seed: dict[str, Any]
    ) -> None:
        org, hoje = triagem_seed["org"], triagem_seed["hoje"]
        end = hoje.replace(hour=23, minute=59, second=59)
        start = end - timedelta(days=200)
        data = compute_atendimento_triagem(org, start=start, end=end)
        assert data["trend_granularity"] == "month"
        # Buckets mensais são bem menos que os 201 dias da janela.
        assert 5 <= len(data["trend"]) <= 9
        assert sum(b["count"] for b in data["trend"]) == data["total"] == 6


# =============================================================================
# Paridade de contagem: altura da barra == total da lista
# =============================================================================
@pytest.mark.django_db
class TestParidadeDeContagem:
    """A barra e a lista têm que sair do MESMO caminho de filtro."""

    def test_departamento_bate_com_a_lista(
        self, triagem_seed: dict[str, Any]
    ) -> None:
        org, hoje, sup = triagem_seed["org"], triagem_seed["hoje"], triagem_seed["sup"]
        start = hoje.replace(hour=0, minute=0)
        end = start + timedelta(days=1, microseconds=-1)

        grafico = compute_atendimento_triagem(org, start=start, end=end)
        barra = next(
            r for r in grafico["by_departamento"] if r["departamento_id"] == sup.id
        )
        lista = compute_atendimento_lista(
            org,
            start=start,
            end=end + timedelta(microseconds=1),  # o que a view faz com `?periodo=`
            foco="todos",
            departamento_id=sup.id,
        )
        assert lista["total"] == barra["total"] == 3

    def test_motivo_bate_com_a_lista(self, triagem_seed: dict[str, Any]) -> None:
        org, hoje = triagem_seed["org"], triagem_seed["hoje"]
        start = hoje.replace(hour=0, minute=0)
        end = start + timedelta(days=1, microseconds=-1)

        grafico = compute_atendimento_triagem(org, start=start, end=end)
        for barra in grafico["top_motivos"]:
            lista = compute_atendimento_lista(
                org,
                start=start,
                end=end + timedelta(microseconds=1),
                foco="todos",
                motivo=barra["motivo"],
            )
            assert lista["total"] == barra["count"], barra["motivo"]

    def test_motivo_repetido_no_mesmo_atendimento_conta_uma_vez(
        self, triagem_seed: dict[str, Any]
    ) -> None:
        """"Upgrade" aparece 2× no mesmo registro; `has_key` da lista conta 1."""
        org, hoje = triagem_seed["org"], triagem_seed["hoje"]
        start = hoje.replace(hour=0, minute=0)
        end = start + timedelta(days=1, microseconds=-1)
        grafico = compute_atendimento_triagem(org, start=start, end=end)
        barra = next(b for b in grafico["top_motivos"] if b["motivo"] == "Upgrade")
        assert barra["count"] == 1


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestParidadeViaHTTP:
    """Mesma paridade, agora pelo caminho real: página → URL do clique → lista."""

    def _barra_e_lista(
        self, client: Any, user: User, org: Organization, query: str,
        param: str, valor: str,
    ) -> tuple[dict[str, Any], int]:
        client.force_login(user)
        pagina = client.get(f"{URL}?{query}")
        assert pagina.status_code == 200
        drill = pagina.context["drill_query"]
        lista = client.get(f"{LISTA_URL}?{param}={valor}&{drill}")
        assert lista.status_code == 200
        return pagina.context, lista.context["total"]

    def test_clique_no_departamento(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        org, sup = triagem_seed["org"], triagem_seed["sup"]
        ctx, total = self._barra_e_lista(
            client, user_a, org, "periodo=1d", "departamento", str(sup.id)
        )
        barra = next(
            r for r in ctx["by_departamento"] if r["departamento_id"] == sup.id
        )
        assert total == barra["total"]

    def test_clique_no_motivo(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        org = triagem_seed["org"]
        ctx, total = self._barra_e_lista(
            client, user_a, org, "periodo=1d", "motivo", "Lentidão"
        )
        barra = next(b for b in ctx["top_motivos"] if b["motivo"] == "Lentidão")
        assert total == barra["count"] == 2

    def test_clique_no_motivo_com_a_pagina_filtrada_por_departamento(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        """Filtro de departamento da página viaja no drill de motivo."""
        sup = triagem_seed["sup"]
        client.force_login(user_a)
        pagina = client.get(f"{URL}?periodo=1d&departamento={sup.id}")
        assert pagina.status_code == 200
        assert f"departamento={sup.id}" in pagina.context["motivo_drill_query"]
        lista = client.get(
            f"{LISTA_URL}?motivo=Lentidão&{pagina.context['motivo_drill_query']}"
        )
        barra = next(
            b for b in pagina.context["top_motivos"] if b["motivo"] == "Lentidão"
        )
        assert lista.context["total"] == barra["count"]


# =============================================================================
# Filtro de período
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPeriodoDaPagina:
    def test_presets_em_dias_na_barra(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        chaves = {p["key"] for p in resp.context["period_presets"]}
        assert {"1d", "ontem", "7d", "30d"} <= chaves
        assert resp.context["period_allow_custom"] is True

    def test_preset_hoje_recorta_o_dia(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?periodo=1d")
        assert resp.context["total"] == 5  # o de 10 dias atrás fica de fora

    def test_months_antigo_continua_funcionando(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        """`?months=1` → `periodo=1m`, pegando também o ruído de 10 dias atrás."""
        client.force_login(user_a)
        resp = client.get(f"{URL}?months=1")
        assert resp.status_code == 200
        assert resp.context["period"].key == "1m"
        assert resp.context["total"] == 6

    def test_periodo_personalizado(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        hoje = triagem_seed["hoje"].date()
        client.force_login(user_a)
        resp = client.get(f"{URL}?de={hoje.isoformat()}&ate={hoje.isoformat()}")
        assert resp.context["period"].is_custom
        assert resp.context["total"] == 5

    def test_voltar_da_lista_preserva_o_periodo(
        self, client: Any, user_a: User, triagem_seed: dict[str, Any]
    ) -> None:
        """Da lista aberta pelo clique, o "voltar" reproduz a tela de origem."""
        sup = triagem_seed["sup"]
        client.force_login(user_a)
        pagina = client.get(f"{URL}?periodo=7d")
        lista = client.get(
            f"{LISTA_URL}?departamento={sup.id}&{pagina.context['drill_query']}"
        )
        assert lista.context["voltar_url"] == URL
        assert lista.context["voltar_label"] == "Atendimento"
        assert "periodo=7d" in lista.context["voltar_query"]

        volta = client.get(f"{lista.context['voltar_url']}?{lista.context['voltar_query']}")
        assert volta.status_code == 200
        assert volta.context["period"].key == "7d"


@pytest.mark.django_db
class TestPeriodoResolvido:
    def test_months_vira_preset_mensal_na_granularidade_em_dias(
        self, rf: Any
    ) -> None:
        """A conversão `?months=N` → `Nm` é do componente; aqui só a garantimos."""
        request = rf.get("/operations/atendimento/?months=3")
        period = get_period(request)
        assert period.key == "3m"
        assert triagem_trend_granularity(period.start, period.end) == "week"


# =============================================================================
# RBAC — pendência herdada da #87
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestRbacDoDrilldown:
    """Quem só tem a aba Atendimento também abre a lista genérica (#89)."""

    def test_so_aba_atendimento_abre_o_drilldown(
        self, client: Any, triagem_seed: dict[str, Any]
    ) -> None:
        org, sup = triagem_seed["org"], triagem_seed["sup"]
        user = _membro_do_grupo(org, email="so-at@a.test", pages=["atendimento"])
        client.force_login(user)

        pagina = client.get(f"{URL}?periodo=7d")
        assert pagina.status_code == 200
        # O clique na barra: mesma URL que o JS monta.
        lista = client.get(
            f"{LISTA_URL}?departamento={sup.id}&{pagina.context['drill_query']}"
        )
        assert lista.status_code == 200
        assert lista.context["voltar_url"] == URL

    def test_so_aba_tendencias_continua_abrindo(
        self, client: Any, triagem_seed: dict[str, Any]
    ) -> None:
        org = triagem_seed["org"]
        user = _membro_do_grupo(
            org, email="so-tend@a.test", pages=["atendimento_tendencias"]
        )
        client.force_login(user)
        assert client.get(f"{LISTA_URL}?periodo=7d").status_code == 200

    def test_quem_nao_tem_nenhuma_das_duas_continua_barrado(
        self, client: Any, triagem_seed: dict[str, Any]
    ) -> None:
        org = triagem_seed["org"]
        user = _membro_do_grupo(org, email="so-exec@a.test", pages=["executive"])
        client.force_login(user)
        resp = client.get(f"{LISTA_URL}?periodo=7d")
        assert resp.status_code == 302
        assert resp.url == "/executive/"
