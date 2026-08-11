"""Testes de qualidade/produção de técnicos (issue #18).

Cobre a função pura `compute_technician_stats` (produção, taxa de solução,
tempo médio, revisitas/retorno, score e ranking) e a view `tecnicos`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.helpdesk.application.technician_stats import compute_technician_stats
from apps.helpdesk.infrastructure.models import OsLookupCache, Ticket
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

_BASE = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _t(
    *,
    tech: str,
    customer: str,
    subject: str,
    status: str = "OPEN",
    opened_days: int = 0,
    resolution_hours: float | None = None,
) -> dict[str, Any]:
    opened_at = _BASE + timedelta(days=opened_days)
    closed_at = (
        opened_at + timedelta(hours=resolution_hours)
        if status == "CLOSED" and resolution_hours is not None
        else None
    )
    return {
        "technician_id": tech,
        "customer_external_id": customer,
        "subject_id": subject,
        "status": status,
        "opened_at": opened_at,
        "closed_at": closed_at,
    }


class TestComputeTechnicianStats:
    def test_production_and_solution_rate(self) -> None:
        tickets = [
            _t(tech="49", customer="c1", subject="10", status="CLOSED", resolution_hours=2),
            _t(tech="49", customer="c2", subject="10", status="CLOSED", resolution_hours=4),
            _t(tech="49", customer="c3", subject="10", status="OPEN"),
        ]
        stats = compute_technician_stats(tickets)
        assert len(stats) == 1
        row = stats[0]
        assert row["total"] == 3
        assert row["closed"] == 2
        assert row["solution_rate"] == pytest.approx(66.7, abs=0.1)
        assert row["avg_res_hours"] == pytest.approx(3.0)  # (2+4)/2
        assert row["top_subject_id"] == "10"

    def test_revisita_attributed_to_previous_tech(self) -> None:
        # Mesmo cliente+assunto, 2ª OS 5 dias depois → revisita do tech da 1ª.
        tickets = [
            _t(tech="49", customer="c1", subject="10", opened_days=0),
            _t(tech="50", customer="c1", subject="10", opened_days=5),
        ]
        stats = compute_technician_stats(tickets, return_window_days=30)
        by_tech = {s["technician_id"]: s for s in stats}
        assert by_tech["49"]["returns"] == 1
        assert by_tech["50"]["returns"] == 0

    def test_revisita_outside_window_ignored(self) -> None:
        tickets = [
            _t(tech="49", customer="c1", subject="10", opened_days=0),
            _t(tech="50", customer="c1", subject="10", opened_days=45),
        ]
        stats = compute_technician_stats(tickets, return_window_days=30)
        by_tech = {s["technician_id"]: s for s in stats}
        assert by_tech["49"]["returns"] == 0

    def test_different_subject_not_revisita(self) -> None:
        tickets = [
            _t(tech="49", customer="c1", subject="10", opened_days=0),
            _t(tech="50", customer="c1", subject="99", opened_days=2),
        ]
        stats = compute_technician_stats(tickets)
        assert all(s["returns"] == 0 for s in stats)

    def test_blank_technician_skipped(self) -> None:
        tickets = [
            _t(tech="", customer="c1", subject="10", status="CLOSED", resolution_hours=1),
            _t(tech="49", customer="c2", subject="10", status="CLOSED", resolution_hours=1),
        ]
        stats = compute_technician_stats(tickets)
        assert {s["technician_id"] for s in stats} == {"49"}

    def test_ranking_sorted_by_score_desc(self) -> None:
        # Tech bom: alta solução, sem retorno. Tech ruim: baixa solução + retorno.
        tickets = [
            _t(tech="good", customer="c1", subject="10", status="CLOSED", resolution_hours=1),
            _t(tech="good", customer="c2", subject="10", status="CLOSED", resolution_hours=1),
            _t(tech="bad", customer="c3", subject="10", status="OPEN", opened_days=0),
            _t(tech="bad", customer="c3", subject="10", status="OPEN", opened_days=3),
        ]
        stats = compute_technician_stats(tickets)
        assert stats[0]["technician_id"] == "good"
        assert stats[0]["score"] >= stats[-1]["score"]


# =============================================================================
# View
# =============================================================================
def _make_ticket(org: Organization, **kw: Any) -> Ticket:
    set_current_organization(org)
    now = timezone.now()
    status = kw.get("status", "OPEN")
    opened_at = now - timedelta(days=kw.get("opened_days", 3))
    closed_at = (
        opened_at + timedelta(hours=kw["resolution_hours"])
        if status == "CLOSED" and "resolution_hours" in kw
        else None
    )
    return Ticket.objects.create(
        organization=org,
        source_type="IXC",
        external_id=kw["external_id"],
        customer_external_id=kw.get("customer", "c1"),
        subject_id=kw.get("subject", "10"),
        technician_id=kw.get("tech", "49"),
        status=status,
        priority="NORMAL",
        protocol=f"P-{kw['external_id']}",
        opened_at=opened_at,
        closed_at=closed_at,
    )


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestTecnicosView:
    def test_requires_login(self, client: Any) -> None:
        resp = client.get("/operations/tecnicos/")
        assert resp.status_code == 302

    def test_renders_ranking(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        OsLookupCache.objects.create(
            organization=organization_a,
            subject_map={"10": "Instalação"},
            technician_map={"49": "Kainan", "50": "Adelso"},
        )
        _make_ticket(organization_a, external_id="1", tech="49", status="CLOSED", resolution_hours=2)
        _make_ticket(organization_a, external_id="2", tech="50", status="OPEN")
        client.force_login(user_a)
        resp = client.get("/operations/tecnicos/")
        assert resp.status_code == 200
        assert b"Kainan" in resp.content
        assert b"Instala" in resp.content  # tipo predominante resolvido

    def test_empty_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/tecnicos/")
        assert resp.status_code == 200
        assert b"Nenhuma OS com t" in resp.content


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPeriodoEmDias:
    """Filtro de período em dias na página (#100)."""

    URL = "/operations/tecnicos/"

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
            subject_map={"10": "Instalação"},
            technician_map={"49": "Kainan", "50": "Adelso"},
        )
        _make_ticket(organization_a, external_id="1", tech="49", opened_days=0)
        _make_ticket(organization_a, external_id="2", tech="50", opened_days=1)

        client.force_login(user_a)
        resp = client.get(f"{self.URL}?periodo=1d")
        assert resp.context["period"].key == "1d"
        assert b"Kainan" in resp.content
        assert b"Adelso" not in resp.content

        resp = client.get(f"{self.URL}?periodo=ontem")
        assert b"Adelso" in resp.content
        assert b"Kainan" not in resp.content

    def test_perfil_propagado_no_personalizado(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(f"{self.URL}?profile=FIELD").content.decode()
        assert 'name="profile" value="FIELD"' in html

    def test_cookie_atravessa_a_navegacao(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        client.get(f"{self.URL}?periodo=7d")
        assert client.get(self.URL).context["period"].key == "7d"


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestGraficoMensalEmJanelaCurta:
    """O único bloco mensal da página some quando a janela não comporta 2 pontos.

    Decisão do épico #100: em vez de desenhar uma "evolução" de um ponto só, o
    gráfico sai e a página explica por quê — o resto continua respeitando o
    período.
    """

    URL = "/operations/tecnicos/"

    def test_janela_curta_esconde_o_grafico_e_avisa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?periodo=7d")
        assert resp.status_code == 200
        assert resp.context["monthly_visivel"] is False
        body = resp.content.decode()
        assert "evolução mês a mês precisa de pelo menos dois meses" in body
        assert 'id="monthly-chart"' not in body

    def test_janela_longa_mantem_o_grafico(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?periodo=6m")
        assert resp.context["monthly_visivel"] is True
        body = resp.content.decode()
        assert 'id="monthly-chart"' in body
        assert "evolução mês a mês precisa" not in body
