"""Testes do dashboard de triagem de atendimento (Opa! Suite) — issue #48.

Cobre a view `atendimento`: exige login, agrega atendimentos por departamento
(volume, % fechados, TMA, nota média), distribuição por status, top motivos,
tendência mensal e o filtro por departamento (?departamento=ID).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _make_departamento(org: Organization, *, external_id: str, nome: str) -> Departamento:
    set_current_organization(org)
    return Departamento.objects.create(
        organization=org,
        source_type="OPA",
        external_id=external_id,
        nome=nome,
        status="A",
    )


def _make_atendimento(
    org: Organization,
    *,
    external_id: str,
    departamento: Departamento | None,
    status: str,
    opened_offset_days: int = 5,
    resolution_hours: float | None = None,
    rating: int | None = None,
    motivos: list[str] | None = None,
) -> Atendimento:
    set_current_organization(org)
    now = timezone.now()
    opened_at = now - timedelta(days=opened_offset_days)
    closed_at = None
    if status == "CLOSED" and resolution_hours is not None:
        closed_at = opened_at + timedelta(hours=resolution_hours)
    return Atendimento.objects.create(
        organization=org,
        source_type="OPA",
        external_id=external_id,
        departamento=departamento,
        departamento_external_id=departamento.external_id if departamento else "",
        status=status,
        canal="whatsapp",
        protocol=f"OPA-{external_id}",
        motivos=motivos or [],
        rating=rating,
        opened_at=opened_at,
        closed_at=closed_at,
    )


@pytest.fixture
def seeded_atendimento(organization_a: Organization) -> Organization:
    suporte = _make_departamento(organization_a, external_id="dep-sup", nome="Suporte")
    comercial = _make_departamento(
        organization_a, external_id="dep-com", nome="Comercial"
    )
    # Suporte: 2 fechados + 1 aberto → 66.7% fechado
    _make_atendimento(
        organization_a, external_id="a1", departamento=suporte, status="CLOSED",
        resolution_hours=2, rating=5, motivos=["lentidão", "sem internet"],
    )
    _make_atendimento(
        organization_a, external_id="a2", departamento=suporte, status="CLOSED",
        resolution_hours=4, rating=3, motivos=["lentidão"],
    )
    _make_atendimento(
        organization_a, external_id="a3", departamento=suporte, status="OPEN",
    )
    # Comercial: 1 fechado
    _make_atendimento(
        organization_a, external_id="a4", departamento=comercial, status="CLOSED",
        resolution_hours=1, rating=4, motivos=["upgrade"],
    )
    return organization_a


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoDashboardView:
    def test_requires_login(self, client: Any) -> None:
        resp = client.get("/operations/atendimento/")
        assert resp.status_code == 302

    def test_renders_with_departamentos(
        self, client: Any, user_a: User, seeded_atendimento: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/atendimento/")
        assert resp.status_code == 200
        assert b"Suporte" in resp.content
        assert b"Comercial" in resp.content
        # Motivos desnormalizados aparecem no payload do gráfico.
        assert b"lentid" in resp.content

    def test_filter_by_departamento(
        self, client: Any, user_a: User, seeded_atendimento: Organization
    ) -> None:
        set_current_organization(seeded_atendimento)
        comercial = Departamento.objects.get(external_id="dep-com")
        client.force_login(user_a)
        resp = client.get(f"/operations/atendimento/?departamento={comercial.id}")
        assert resp.status_code == 200
        # Filtrando por Comercial: badge do filtro aparece.
        assert b"Filtrando por" in resp.content
        assert b"upgrade" in resp.content
        # Motivo exclusivo de Suporte não entra no recorte filtrado.
        assert b"sem internet" not in resp.content

    def test_empty_org_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/operations/atendimento/")
        assert resp.status_code == 200
        assert b"Nenhum atendimento no per" in resp.content
