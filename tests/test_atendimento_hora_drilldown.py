"""Drill-down por clique no gráfico horário (#77).

Cobre o helper de resumo da hora (`atendimento_hora_resumo`), o "esperado" do
slot (`atendimento_hora_esperado` — mesmo baseline do gráfico, janela de um
ponto só), o `customdata` com o ISO do slot no JSON do Plotly e a navegação
(setas hora anterior/próxima + volta pras Tendências com os filtros).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    atendimento_hora_esperado,
    atendimento_hora_resumo,
    compute_atendimento_hora,
    compute_atendimento_horario,
)
from apps.atendimento.infrastructure.models import Atendimento
from apps.dashboards import charts
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

_SP = ZoneInfo("America/Sao_Paulo")
URL = "/operations/atendimento-hora/"
TENDENCIAS_URL = "/operations/atendimento-tendencias/"


def _hour(days_ago: int = 2, hour: int = 14) -> datetime:
    local = timezone.localtime(timezone.now(), _SP)
    return (local - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _h_param(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _at(
    org: Organization,
    *,
    external_id: str,
    opened_at: datetime,
    atendente_nome: str = "",
    canal: str = "whatsapp",
    status: str = Atendimento.Status.CLOSED.value,
    tags: list[str] | None = None,
    motivos: list[str] | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=status,
        opened_at=opened_at,
        canal=canal,
        atendente_nome=atendente_nome,
        tags=tags or [],
        motivos=motivos or [],
    )


def _row(
    *,
    categorias: list[str],
    atendente: str,
    status: str = "Finalizado",
    canal: str = "whatsapp",
) -> dict[str, Any]:
    """Linha no formato de `compute_atendimento_hora`, só com o que o resumo lê."""
    return {
        "categorias": categorias,
        "atendente_nome": atendente,
        "status_label": status,
        "canal": canal,
    }


class TestAtendimentoHoraResumo:
    """Helper puro — conta a partir das mesmas linhas exibidas na lista."""

    def _rows(self) -> list[dict[str, Any]]:
        return [
            _row(categorias=["Quedas", "LOS"], atendente="Ana"),
            _row(categorias=["Quedas"], atendente="Ana"),
            _row(categorias=["Quedas", "Lentidão"], atendente="Bruno"),
            _row(
                categorias=["Lentidão"], atendente="Bruno",
                status="Aberto", canal="telefone",
            ),
            _row(categorias=[], atendente="Ana", status="Aberto"),
        ]

    def test_total_e_top_categorias(self) -> None:
        r = atendimento_hora_resumo(self._rows())
        assert r["total"] == 5
        assert r["n_categorias"] == 3
        nomes = [c["nome"] for c in r["top_categorias"]]
        assert nomes == ["Quedas", "Lentidão", "LOS"]
        assert r["top_categorias"][0]["count"] == 3
        assert r["top_categorias"][0]["pct"] == 60.0
        assert r["top_categorias"][2]["count"] == 1
        assert r["top_categorias"][2]["pct"] == 20.0

    def test_top_categorias_limita_em_cinco(self) -> None:
        rows = [
            _row(categorias=[f"c{i}" for i in range(8)], atendente="Ana"),
        ]
        r = atendimento_hora_resumo(rows)
        assert r["n_categorias"] == 8
        assert len(r["top_categorias"]) == 5

    def test_atendentes_status_e_canal(self) -> None:
        r = atendimento_hora_resumo(self._rows())
        assert r["n_atendentes"] == 2
        assert [(a["nome"], a["count"]) for a in r["atendentes"]] == [
            ("Ana", 3), ("Bruno", 2)
        ]
        assert [(s["nome"], s["count"]) for s in r["status_dist"]] == [
            ("Finalizado", 3), ("Aberto", 2)
        ]
        assert [(c["nome"], c["count"]) for c in r["canal_dist"]] == [
            ("whatsapp", 4), ("telefone", 1)
        ]
        assert sum(s["count"] for s in r["status_dist"]) == 5

    def test_hora_vazia_nao_quebra(self) -> None:
        r = atendimento_hora_resumo([])
        assert r["total"] == 0
        assert r["top_categorias"] == []
        assert r["atendentes"] == []


@pytest.mark.django_db
class TestResumoNoComputeDaHora:
    def test_resumo_bate_com_as_linhas(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="a1", opened_at=h,
            atendente_nome="Ana", motivos=["Quedas"])
        _at(organization_a, external_id="a2", opened_at=h + timedelta(minutes=10),
            atendente_nome="Ana", tags=["Quedas", "LOS"])
        # Fora da hora — não pode entrar no resumo.
        _at(organization_a, external_id="a3", opened_at=h + timedelta(hours=2),
            atendente_nome="Bruno", motivos=["Quedas"])

        data = compute_atendimento_hora(organization_a, hour_start=h, foco="todos")
        resumo = data["resumo"]
        assert resumo["total"] == data["total"] == 2
        assert resumo["top_categorias"][0]["nome"] == "Quedas"
        assert resumo["top_categorias"][0]["count"] == 2
        assert [a["nome"] for a in resumo["atendentes"]] == ["Ana"]


@pytest.mark.django_db
class TestAtendimentoHoraEsperado:
    def test_esperado_igual_ao_slot_do_grafico(
        self, organization_a: Organization
    ) -> None:
        """O valor tem que ser o MESMO do ponto do gráfico da janela inteira."""
        set_current_organization(organization_a)
        h = _hour()
        # Baseline: 6 semanas anteriores com 2 atendimentos no mesmo slot.
        for w in range(1, 7):
            base = h - timedelta(weeks=w)
            for i in range(2):
                _at(organization_a, external_id=f"b{w}-{i}",
                    opened_at=base + timedelta(minutes=i))
        # Hora analisada: pico de 8.
        for i in range(8):
            _at(organization_a, external_id=f"p{i}",
                opened_at=h + timedelta(minutes=i))

        grafico = compute_atendimento_horario(organization_a, days=7, foco="todos")
        idx = grafico["labels"].index(h.strftime("%d/%m %Hh"))

        esperado = atendimento_hora_esperado(
            organization_a, hour_start=h, foco="todos"
        )
        assert esperado["esperado"] == grafico["expected"][idx]
        assert esperado["upper"] == grafico["upper"][idx]
        assert esperado["real"] == grafico["actual"][idx] == 8
        assert esperado["ratio"] == round(8 / grafico["expected"][idx], 1)

    def test_baseline_zerado_nao_gera_razao(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="a1", opened_at=h)
        esperado = atendimento_hora_esperado(
            organization_a, hour_start=h, foco="todos"
        )
        assert esperado["esperado"] == 0.0
        assert esperado["ratio"] is None


@pytest.mark.django_db
class TestCustomdataNoGrafico:
    def test_slots_alinhados_com_labels(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        d = compute_atendimento_horario(organization_a, days=7, foco="todos")
        assert len(d["slots"]) == len(d["labels"]) == d["n_slots"]
        # ISO local aceito pelo `?h=` da página da hora.
        primeiro = datetime.fromisoformat(d["slots"][0])
        assert primeiro.minute == 0
        assert primeiro.strftime("%d/%m %Hh") == d["labels"][0]

    def test_customdata_no_json_do_plotly(self) -> None:
        d = {
            "labels": ["03/08 13h", "03/08 14h"],
            "slots": ["2026-08-03T13:00", "2026-08-03T14:00"],
            "actual": [3, 37],
            "expected": [3.0, 9.0],
            "upper": [6.0, 15.0],
            "lower": [0.0, 3.0],
            "anomaly_x": ["03/08 14h"],
            "anomaly_y": [37],
            "anomaly_slots": ["2026-08-03T14:00"],
            "detect": "spike",
            "billing_day_labels": [],
            "vencimentos": [],
        }
        fig = json.loads(charts.atendimento_horario_sazonal(d))
        por_nome = {t.get("name"): t for t in fig["data"]}
        assert list(por_nome["Real"]["customdata"]) == [
            "2026-08-03T13:00", "2026-08-03T14:00",
        ]
        assert list(por_nome["Anomalia"]["customdata"]) == ["2026-08-03T14:00"]
        # Invariante que o handler do clique precisa respeitar: com
        # `hovermode: "x unified"` o evento traz um ponto de CADA trace daquele
        # x, e o primeiro deles ("Esperado") NÃO tem customdata — por isso o JS
        # procura o primeiro ponto COM customdata em vez de usar `points[0]`.
        assert fig["layout"]["hovermode"] == "x unified"
        assert "customdata" not in por_nome["Esperado"]
        assert [t.get("name") for t in fig["data"]].index("Esperado") < [
            t.get("name") for t in fig["data"]
        ].index("Real")


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestViewDrilldown:
    def test_resumo_renderiza(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="a1", opened_at=h,
            atendente_nome="Ana Silva", motivos=["Quedas"])
        client.force_login(user_a)
        html = client.get(f"{URL}?h={_h_param(h)}&foco=todos").content.decode()
        assert "Volume" in html
        assert "Principais categorias" in html
        assert "Quedas" in html
        assert "Ana Silva" in html
        assert "Distribuição" in html
        assert "whatsapp" in html

    def test_setas_de_hora_preservam_filtros(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        h = _hour()
        client.force_login(user_a)
        resp = client.get(f"{URL}?h={_h_param(h)}&foco=rede&periodo=7d")
        html = resp.content.decode()
        anterior = _h_param(h - timedelta(hours=1))
        seguinte = _h_param(h + timedelta(hours=1))
        assert f"h={anterior}&amp;periodo=7d&amp;foco=rede" in html
        assert f"h={seguinte}&amp;periodo=7d&amp;foco=rede" in html
        # Volta pras Tendências mantém período e foco.
        assert f'href="{TENDENCIAS_URL}?periodo=7d&amp;foco=rede"' in html

    def test_hora_futura_nao_vira_link(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        agora = timezone.localtime(timezone.now(), _SP).replace(
            minute=0, second=0, microsecond=0
        )
        client.force_login(user_a)
        html = client.get(f"{URL}?h={_h_param(agora)}&foco=todos").content.decode()
        proxima = _h_param(agora + timedelta(hours=1))
        assert f"h={proxima}" not in html

    def test_tendencias_tem_handler_de_clique(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(f"{TENDENCIAS_URL}?periodo=7d&foco=rede").content.decode()
        assert "plotly_click" in html
        assert URL in html
        assert "foco=rede" in html
