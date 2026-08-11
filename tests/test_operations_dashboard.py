"""Testes da página Operações — Chamados (#100).

Antes desta issue o seletor de período desta página governava **só** o gráfico
de volume: os KPIs eram do mês corrente hardcoded e o SLA por tipo era fixo em
30 dias. Estes testes prendem o comportamento novo — tudo o que tem janela
segue o filtro, e o que é estoque (chamados em aberto) fica marcado na UI como
"posição de agora" em vez de fingir que responde ao período.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.helpdesk.infrastructure.models import Ticket
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

URL = "/operations/"


def _ticket(
    org: Organization,
    *,
    external_id: str,
    status: str = "CLOSED",
    opened_offset_days: int = 3,
    resolution_hours: float | None = 1.0,
) -> Ticket:
    set_current_organization(org)
    opened_at = timezone.now() - timedelta(days=opened_offset_days)
    closed_at = (
        opened_at + timedelta(hours=resolution_hours)
        if status == "CLOSED" and resolution_hours is not None
        else None
    )
    return Ticket.objects.create(
        organization=org,
        source_type="IXC",
        external_id=external_id,
        customer_external_id="c1",
        subject_id="10",
        technician_id="49",
        status=status,
        priority="NORMAL",
        protocol=f"P-{external_id}",
        opened_at=opened_at,
        closed_at=closed_at,
    )


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPeriodoEmDias:
    def test_requires_login(self, client: Any) -> None:
        assert client.get(URL).status_code == 302

    def test_barra_tem_presets_em_dias_e_personalizado(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "setPeriodo('1d')" in html
        assert "setPeriodo('ontem')" in html
        assert 'name="de"' in html
        assert resp.context["period"].key == "30d"
        assert resp.context["period_warning"] is None

    def test_kpi_de_fechados_segue_o_filtro_e_nao_o_mes_corrente(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        # Um fechado ontem, um fechado há 10 dias: os dois caem no mês corrente
        # na maior parte do mês, mas só um cai na janela "Ontem".
        _ticket(organization_a, external_id="1", opened_offset_days=1)
        _ticket(organization_a, external_id="2", opened_offset_days=10)

        client.force_login(user_a)
        resp = client.get(f"{URL}?periodo=ontem")
        assert resp.context["period"].key == "ontem"
        assert resp.context["closed_in_period"] == 1

        resp = client.get(f"{URL}?periodo=30d")
        assert resp.context["closed_in_period"] == 2

    def test_sla_por_tipo_usa_a_janela_do_filtro(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        assert client.get(f"{URL}?periodo=7d").context["sla_dias"] == 7
        assert client.get(f"{URL}?periodo=1d").context["sla_dias"] == 1

    def test_cookie_atravessa_a_navegacao(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        client.get(f"{URL}?periodo=7d")
        assert client.get(URL).context["period"].key == "7d"


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestIndicadoresDeEscopo:
    """O que não responde ao período fica marcado, não escondido (#100)."""

    def test_blocos_de_estoque_ganham_o_selo_posicao_de_agora(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _ticket(organization_a, external_id="1", status="OPEN", resolution_hours=None)
        client.force_login(user_a)
        body = client.get(URL).content.decode()
        # Chamados abertos, distribuição por prioridade e fila de antigos.
        assert body.count("posição de agora") == 3

    def test_chamados_abertos_ignoram_a_janela_de_proposito(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        # Aberto há 300 dias: fora de qualquer janela curta, mas continua sendo
        # estoque em aberto hoje — tem que aparecer no KPI mesmo com "Ontem".
        _ticket(
            organization_a, external_id="1", status="OPEN",
            opened_offset_days=300, resolution_hours=None,
        )
        client.force_login(user_a)
        assert client.get(f"{URL}?periodo=ontem").context["open_count"] == 1

    def test_volume_mensal_some_em_janela_curta_e_avisa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?periodo=7d")
        assert resp.context["volume_visivel"] is False
        body = resp.content.decode()
        assert "volume mês a mês precisa de pelo menos dois meses" in body
        assert 'id="volume-chart"' not in body

    def test_volume_mensal_volta_em_janela_longa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?periodo=6m")
        assert resp.context["volume_visivel"] is True
        assert 'id="volume-chart"' in resp.content.decode()
