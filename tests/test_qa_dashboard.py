"""Testes do dashboard de QA de atendimento (scorecard LLM-as-judge) — issue #51.

Cobre a agregação `compute_qa_overview` (KPIs, scorecard por atendente,
categorias, piores conversas + filtro por departamento) e a view `qa_supervisor`,
mais o bloco de QA no drill-down `atendimento_detail`. Nenhum teste toca a rede —
os `QAReview` são semeados direto no banco (o juiz LLM não é chamado aqui).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_qa_overview
from apps.analytics.infrastructure.models import QAReview
from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _departamento(org: Organization, *, external_id: str, nome: str) -> Departamento:
    set_current_organization(org)
    return Departamento.objects.create(
        organization=org, source_type="OPA", external_id=external_id, nome=nome,
    )


def _atendimento(
    org: Organization,
    *,
    external_id: str,
    departamento: Departamento | None = None,
    atendente_external_id: str = "ag-1",
    atendente_nome: str = "Atendente Maria",
    opened_offset_days: int = 5,
) -> Atendimento:
    set_current_organization(org)
    opened_at = timezone.now() - timedelta(days=opened_offset_days)
    return Atendimento.objects.create(
        organization=org, source_type="OPA", external_id=external_id,
        departamento=departamento,
        departamento_external_id=departamento.external_id if departamento else "",
        customer_name=f"Cliente {external_id}", customer_document="",
        atendente_external_id=atendente_external_id, atendente_nome=atendente_nome,
        status="CLOSED", canal="whatsapp", protocol=f"OPA-{external_id}",
        opened_at=opened_at, closed_at=opened_at + timedelta(hours=1),
    )


def _review(
    org: Organization,
    at: Atendimento,
    *,
    overall_score: int,
    tom: int = 4,
    empatia: int = 4,
    aderencia: int = 3,
    resolveu: bool = True,
    sla_ok: bool = True,
    categoria: str = "sem conexão",
    melhoria: str = "Confirmar a solução no fim.",
) -> QAReview:
    set_current_organization(org)
    return QAReview.objects.create(
        organization=org, atendimento=at,
        resolveu=resolveu, sla_ok=sla_ok, tom=tom, empatia=empatia,
        aderencia=aderencia, overall_score=overall_score, categoria=categoria,
        resumo="Resumo.", melhoria=melhoria,
        atendente_external_id=at.atendente_external_id,
        atendente_nome=at.atendente_nome, model_name="gemini-2.0-flash",
        reviewed_at=timezone.now(),
    )


@pytest.fixture
def seeded(organization_a: Organization) -> dict[str, Any]:
    suporte = _departamento(organization_a, external_id="dep-sup", nome="Suporte")
    ouvidoria = _departamento(organization_a, external_id="dep-ouv", nome="Ouvidoria")

    # Maria (Suporte): duas avaliações boas.
    m1 = _atendimento(organization_a, external_id="m1", departamento=suporte)
    _review(organization_a, m1, overall_score=80)
    m2 = _atendimento(organization_a, external_id="m2", departamento=suporte)
    _review(organization_a, m2, overall_score=90, categoria="lentidão")

    # João (Ouvidoria): uma avaliação ruim → deve liderar o scorecard (pior).
    joao = _atendimento(
        organization_a, external_id="j1", departamento=ouvidoria,
        atendente_external_id="ag-2", atendente_nome="Atendente João",
    )
    _review(
        organization_a, joao, overall_score=30, tom=2, empatia=2,
        resolveu=False, sla_ok=False, categoria="cancelamento",
        melhoria="Demonstrar mais empatia.",
    )
    return {"suporte": suporte, "ouvidoria": ouvidoria, "joao_at": joao}


@pytest.mark.django_db
class TestComputeQaOverview:
    def test_kpis_and_atendente_scorecard(
        self, organization_a: Organization, seeded: dict[str, Any]
    ) -> None:
        data = compute_qa_overview(organization_a, months=3)
        assert data["total_reviews"] == 3
        # média (80+90+30)/3 ≈ 67
        assert data["avg_score"] == 67
        # 2 de 3 resolveram / SLA ok
        assert data["pct_resolveu"] == 67
        assert data["pct_sla"] == 67
        # Scorecard ordenado pelo pior score primeiro → João lidera.
        nomes = [a["atendente_nome"] for a in data["by_atendente"]]
        assert nomes[0] == "Atendente João"
        joao = data["by_atendente"][0]
        assert joao["avg_score"] == 30
        assert joao["pct_resolveu"] == 0

    def test_categorias_and_worst(
        self, organization_a: Organization, seeded: dict[str, Any]
    ) -> None:
        data = compute_qa_overview(organization_a, months=3)
        cats = {c["categoria"] for c in data["categorias"]}
        assert {"sem conexão", "lentidão", "cancelamento"} <= cats
        # Piores ordenadas por menor score → cancelamento (30) primeiro.
        assert data["worst"][0]["overall_score"] == 30
        assert data["worst"][0]["categoria"] == "cancelamento"

    def test_filter_by_departamento(
        self, organization_a: Organization, seeded: dict[str, Any]
    ) -> None:
        data = compute_qa_overview(
            organization_a, months=3, departamento_id=seeded["ouvidoria"].id
        )
        assert data["total_reviews"] == 1
        assert data["by_atendente"][0]["atendente_nome"] == "Atendente João"

    def test_empty_org(self, organization_a: Organization) -> None:
        data = compute_qa_overview(organization_a, months=3)
        assert data["total_reviews"] == 0
        assert data["avg_score"] == 0
        assert data["by_atendente"] == []
        assert data["worst"] == []


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestQaSupervisorView:
    def test_requires_login(self, client: Any) -> None:
        assert client.get("/operations/qa/").status_code == 302

    def test_renders_scorecard(
        self, client: Any, user_a: User, seeded: dict[str, Any]
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/qa/")
        assert resp.status_code == 200
        assert b"Atendente Jo" in resp.content
        assert b"Atendente Maria" in resp.content
        assert b"Demonstrar mais empatia" in resp.content

    def test_empty_org_renders_placeholder(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/qa/")
        assert resp.status_code == 200
        assert b"Nenhuma conversa avaliada" in resp.content


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestQaBlockInDetail:
    def test_detail_shows_qa_review(
        self, client: Any, user_a: User, seeded: dict[str, Any]
    ) -> None:
        at = seeded["joao_at"]
        client.force_login(user_a)
        resp = client.get(f"/operations/conversas-ruins/{at.id}/")
        assert resp.status_code == 200
        assert b"Avalia" in resp.content  # bloco "Avaliação da IA supervisora"
        assert b"Demonstrar mais empatia" in resp.content
