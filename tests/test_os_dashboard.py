"""Testes do dashboard de Ordens de Serviço — análise por tipo de OS (issue #17).

Cobre a view `os_dashboard`: exige login, agrega OS por tipo (subject_id →
assunto via OsLookupCache), calcula taxa de solução e tempo médio de resolução,
e cai num fallback legível quando os lookups não foram sincronizados.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.helpdesk.infrastructure.models import OsLookupCache, Ticket
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _make_ticket(
    org: Organization,
    *,
    external_id: str,
    subject_id: str,
    status: str,
    opened_offset_days: int = 5,
    resolution_hours: float | None = None,
) -> Ticket:
    set_current_organization(org)
    now = timezone.now()
    opened_at = now - timedelta(days=opened_offset_days)
    closed_at = None
    if status == "CLOSED" and resolution_hours is not None:
        closed_at = opened_at + timedelta(hours=resolution_hours)
    return Ticket.objects.create(
        organization=org,
        source_type="IXC",
        external_id=external_id,
        customer_external_id="c-1",
        subject_id=subject_id,
        status=status,
        priority="NORMAL",
        protocol=f"P-{external_id}",
        opened_at=opened_at,
        closed_at=closed_at,
    )


@pytest.fixture
def seeded_os(organization_a: Organization) -> Organization:
    OsLookupCache.objects.create(
        organization=organization_a,
        subject_map={"10": "Instalação", "164": "Inclusão SPC"},
        technician_map={"49": "Pablo Técnico"},
    )
    # Tipo 10 (Instalação): 2 fechadas + 1 aberta → taxa 66.7%
    _make_ticket(organization_a, external_id="1", subject_id="10", status="CLOSED", resolution_hours=2)
    _make_ticket(organization_a, external_id="2", subject_id="10", status="CLOSED", resolution_hours=4)
    _make_ticket(organization_a, external_id="3", subject_id="10", status="OPEN")
    # Tipo 164 (Inclusão SPC): 1 fechada
    _make_ticket(organization_a, external_id="4", subject_id="164", status="CLOSED", resolution_hours=1)
    return organization_a


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestOsDashboardView:
    def test_requires_login(self, client: Any) -> None:
        resp = client.get("/operations/os/")
        assert resp.status_code == 302

    def test_renders_with_types(
        self, client: Any, user_a: User, seeded_os: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/os/")
        assert resp.status_code == 200
        # Nomes de assunto resolvidos via OsLookupCache.
        assert b"Insta" in resp.content  # "Instalação"
        assert b"Inclus" in resp.content  # "Inclusão SPC"

    def test_falls_back_without_lookups(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        # Sem OsLookupCache: assuntos exibem fallback legível "Assunto #X".
        _make_ticket(organization_a, external_id="1", subject_id="10", status="OPEN")
        client.force_login(user_a)
        resp = client.get("/operations/os/")
        assert resp.status_code == 200
        assert b"Assunto #10" in resp.content
        # Banner de não-sincronizado aparece.
        assert b"sync_os_lookups" in resp.content

    def test_empty_org_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/os/")
        assert resp.status_code == 200
        assert b"Nenhuma OS no per" in resp.content


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPeriodoEmDias:
    """Filtro de período em dias na página (#100)."""

    URL = "/operations/os/"

    def test_barra_tem_presets_em_dias_e_personalizado(
        self, client: Any, user_a: User, organization_a: Organization
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

    def test_janela_corta_nos_dois_lados(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        OsLookupCache.objects.create(
            organization=organization_a,
            subject_map={"10": "Instalação", "20": "Reparo"},
            technician_map={},
        )
        _make_ticket(
            organization_a, external_id="1", subject_id="10", status="CLOSED",
            opened_offset_days=0, resolution_hours=1,
        )
        _make_ticket(
            organization_a, external_id="2", subject_id="20", status="CLOSED",
            opened_offset_days=1, resolution_hours=1,
        )

        client.force_login(user_a)
        resp = client.get(f"{self.URL}?periodo=1d")
        assert resp.context["period"].key == "1d"
        assert resp.context["total_os"] == 1
        assert b"Instala" in resp.content
        assert b"Reparo" not in resp.content

        resp = client.get(f"{self.URL}?periodo=ontem")
        assert resp.context["total_os"] == 1
        assert b"Reparo" in resp.content

    def test_cookie_atravessa_a_navegacao(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        client.get(f"{self.URL}?periodo=7d")
        assert client.get(self.URL).context["period"].key == "7d"


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestTendenciaMensalEmJanelaCurta:
    """A tendência mensal some quando a janela não comporta 2 pontos (#100)."""

    URL = "/operations/os/"

    def test_janela_curta_esconde_o_grafico_e_avisa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?periodo=7d")
        assert resp.status_code == 200
        assert resp.context["trend_visivel"] is False
        body = resp.content.decode()
        assert "tendência mês a mês precisa de pelo menos dois meses" in body
        assert 'id="os-trend-chart"' not in body

    def test_janela_longa_mantem_o_grafico(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?periodo=6m")
        assert resp.context["trend_visivel"] is True
        body = resp.content.decode()
        assert 'id="os-trend-chart"' in body
        assert "tendência mês a mês precisa" not in body
