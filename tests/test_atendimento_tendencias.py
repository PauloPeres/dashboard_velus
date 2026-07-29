"""Testes de `compute_atendimento_tendencias` (F2 — motivos/tags no tempo).

Cobre bucketing semanal/mensal, contagem de N motivos/tags por atendimento,
top_n + "Outros", eixo de buckets completo e filtro por departamento.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_atendimento_tendencias
from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _at(
    org: Organization,
    *,
    external_id: str,
    opened_at: datetime,
    motivos: list[str] | None = None,
    tags: list[str] | None = None,
    departamento: Departamento | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
        motivos=motivos or [],
        tags=tags or [],
        departamento=departamento,
    )


def _series_map(series: list[dict]) -> dict[str, list[int]]:
    return {s["name"]: s["values"] for s in series}


@pytest.mark.django_db
class TestAtendimentoTendencias:
    def test_counts_each_tag_of_multi_tag_atendimento(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        _at(
            organization_a,
            external_id="a1",
            opened_at=now - timedelta(days=3),
            tags=["Suporte", "Comercial"],
            motivos=["Sem conexão"],
        )
        _at(
            organization_a,
            external_id="a2",
            opened_at=now - timedelta(days=3),
            tags=["Suporte"],
        )

        data = compute_atendimento_tendencias(
            organization_a, months=6, granularity="week"
        )
        tags = _series_map(data["tags_series"])
        # Suporte aparece em 2 atendimentos, Comercial em 1 — soma por categoria.
        assert sum(tags["Suporte"]) == 2
        assert sum(tags["Comercial"]) == 1
        # Um atendimento com >=1 tag conta 1 vez em atendimentos_com_tag.
        assert data["atendimentos_com_tag"] == 2
        assert sum(_series_map(data["motivos_series"])["Sem conexão"]) == 1

    def test_top_n_plus_outros(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        # 3 tags distintas com frequências A=3, B=2, C=1; top_n=2 -> C vira "Outros".
        seed = ["A", "A", "A", "B", "B", "C"]
        for i, t in enumerate(seed):
            _at(organization_a, external_id=f"x{i}", opened_at=now - timedelta(days=2),
                tags=[t])

        data = compute_atendimento_tendencias(
            organization_a, months=6, granularity="week", top_n=2
        )
        names = [s["name"] for s in data["tags_series"]]
        assert names[:2] == ["A", "B"]  # ordenado por frequência
        assert "Outros" in names
        outros = _series_map(data["tags_series"])["Outros"]
        assert sum(outros) == 1  # só o C
        assert data["n_tags_distintas"] == 3

    def test_top_n_none_shows_all_without_outros(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        for i, t in enumerate(["A", "A", "B", "C", "D"]):
            _at(organization_a, external_id=f"x{i}", opened_at=now - timedelta(days=2),
                tags=[t])

        data = compute_atendimento_tendencias(
            organization_a, months=6, granularity="week", top_n=None
        )
        names = [s["name"] for s in data["tags_series"]]
        assert "Outros" not in names
        assert set(names) == {"A", "B", "C", "D"}  # todas as categorias

    def test_weekly_vs_monthly_bucket_count(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        _at(organization_a, external_id="a1", opened_at=now - timedelta(days=5),
            tags=["X"])

        weekly = compute_atendimento_tendencias(
            organization_a, months=6, granularity="week"
        )
        monthly = compute_atendimento_tendencias(
            organization_a, months=6, granularity="month"
        )
        # Eixo completo de ~6 meses: semanal tem muito mais buckets que mensal.
        assert len(weekly["buckets"]) > len(monthly["buckets"])
        assert len(monthly["buckets"]) in (6, 7)  # 6 meses de janela

    def test_empty_buckets_are_zero_filled(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        _at(organization_a, external_id="a1", opened_at=now - timedelta(days=1),
            tags=["X"])

        data = compute_atendimento_tendencias(
            organization_a, months=6, granularity="month"
        )
        x_vals = _series_map(data["tags_series"])["X"]
        # Uma série por categoria com um valor por bucket; só o último bucket tem 1.
        assert len(x_vals) == len(data["buckets"])
        assert sum(x_vals) == 1
        assert x_vals[-1] == 1
        assert x_vals[0] == 0

    def test_departamento_filter(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        dep = Departamento.objects.create(
            organization=organization_a,
            source_type=SourceType.OPA.value,
            external_id="dep1",
            nome="Suporte",
        )
        _at(organization_a, external_id="a1", opened_at=now - timedelta(days=1),
            tags=["X"], departamento=dep)
        _at(organization_a, external_id="a2", opened_at=now - timedelta(days=1),
            tags=["Y"])  # sem departamento

        data = compute_atendimento_tendencias(
            organization_a, months=6, granularity="week", departamento_id=dep.id
        )
        names = [s["name"] for s in data["tags_series"]]
        assert names == ["X"]
        assert data["total"] == 1
        assert data["selected_departamento_nome"] == "Suporte"

    def test_invalid_granularity_falls_back_to_week(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        data = compute_atendimento_tendencias(
            organization_a, months=6, granularity="daily"
        )
        assert data["granularity"] == "week"


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoTendenciasView:
    URL = "/operations/atendimento-tendencias/"

    def test_requires_login(self, client: Any) -> None:
        assert client.get(self.URL).status_code == 302

    def test_renders_with_data(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        _at(
            organization_a,
            external_id="a1",
            opened_at=timezone.now() - timedelta(days=2),
            motivos=["Sem conexão"],
            tags=["Suporte"],
        )
        client.force_login(user_a)
        resp = client.get(self.URL)
        assert resp.status_code == 200
        # Nomes de motivo/tag chegam no JSON dos gráficos renderizados.
        assert b"Suporte" in resp.content
        assert b"Sem conex" in resp.content
        assert b"Semanal" in resp.content  # toggle presente

    def test_monthly_granularity(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?g=month")
        assert resp.status_code == 200

    def test_empty_org_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        assert client.get(self.URL).status_code == 200
