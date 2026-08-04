"""Registro manual de eventos de rede + overlay no gráfico horário (#78).

Cobre o model (cross-tenant leak), o CRUD pela página de Tendências (permissão
owner-only, escopo de organização), o recorte de janela (só eventos que
intersectam a janela exibida chegam ao gráfico — incluindo a borda) e as shapes
correspondentes no JSON do Plotly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_atendimento_eventos_rede,
    compute_atendimento_horario,
)
from apps.atendimento.infrastructure.models import EventoRede
from apps.dashboards import charts
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationMembership, User

_SP = ZoneInfo("America/Sao_Paulo")
NOVO_URL = "/operations/eventos-rede/novo/"
TENDENCIAS_URL = "/operations/atendimento-tendencias/"


def _hour(days_ago: int = 2, hour: int = 14) -> datetime:
    local = timezone.localtime(timezone.now(), _SP)
    return (local - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _evento(
    org: Organization,
    *,
    titulo: str = "Rompimento troncal",
    tipo: str = EventoRede.Tipo.ROMPIMENTO.value,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> EventoRede:
    """Cria um evento da org (o TenantManager exige org no contexto)."""
    set_current_organization(org)
    try:
        return EventoRede.objects.create(
            organization=org,
            tipo=tipo,
            titulo=titulo,
            started_at=started_at or _hour(),
            ended_at=ended_at,
        )
    finally:
        set_current_organization(None)


def _qs(org: Organization) -> Any:
    """Queryset escopado — contextvar setado explicitamente pro TenantManager."""
    set_current_organization(org)
    return EventoRede.objects.all()


def _member(org: Organization, *, email: str, role: str = "MEMBER") -> User:
    u = User.objects.create_user(email=email)
    OrganizationMembership.objects.create(
        user=u, organization=org, role=role, is_active=True,
    )
    return u


def _dt_param(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


@pytest.mark.django_db
class TestEventoRedeModel:
    def test_str_e_propriedades(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        inicio = _hour()
        ev = _evento(organization_a, started_at=inicio)
        assert "Rompimento" in str(ev)
        assert ev.titulo in str(ev)
        assert ev.is_pontual is True
        assert ev.cor == EventoRede.CORES[EventoRede.Tipo.ROMPIMENTO.value]

        ev.ended_at = inicio + timedelta(hours=2)
        ev.save(update_fields=["ended_at"])
        assert ev.is_pontual is False

    def test_no_cross_tenant_leak(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        _evento(organization_a, titulo="Evento A")
        _evento(organization_b, titulo="Evento B")

        set_current_organization(organization_a)
        titulos = list(EventoRede.objects.values_list("titulo", flat=True))
        assert titulos == ["Evento A"]

        set_current_organization(organization_b)
        titulos = list(EventoRede.objects.values_list("titulo", flat=True))
        assert titulos == ["Evento B"]

    def test_historico_registrado(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        ev = _evento(organization_a)
        ev.titulo = "Rompimento troncal (revisado)"
        ev.save(update_fields=["titulo"])
        assert ev.history.count() == 2


@pytest.mark.django_db
class TestJanelaDoGrafico:
    """Só eventos que intersectam a janela exibida chegam ao gráfico."""

    def _janela(self) -> tuple[datetime, datetime]:
        fim = timezone.localtime(timezone.now(), _SP).replace(
            minute=0, second=0, microsecond=0
        )
        return fim - timedelta(days=3), fim

    def test_evento_dentro_da_janela_entra(
        self, organization_a: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_a, started_at=ws + timedelta(hours=5),
                ended_at=ws + timedelta(hours=7))
        eventos = compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        )
        assert [e["titulo"] for e in eventos] == ["Rompimento troncal"]

    def test_evento_anterior_a_janela_fica_de_fora(
        self, organization_a: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_a, started_at=ws - timedelta(days=2),
                ended_at=ws - timedelta(days=1))
        assert compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        ) == []

    def test_borda_evento_que_termina_no_inicio_da_janela_fica_de_fora(
        self, organization_a: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_a, started_at=ws - timedelta(hours=3), ended_at=ws)
        assert compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        ) == []

    def test_borda_evento_que_termina_logo_apos_o_inicio_entra_clampado(
        self, organization_a: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_a, started_at=ws - timedelta(hours=3),
                ended_at=ws + timedelta(minutes=1))
        eventos = compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        )
        assert len(eventos) == 1
        # Início antes da janela é clampado no primeiro slot exibido.
        assert eventos[0]["slot_start"] == ws.strftime("%Y-%m-%dT%H:%M")
        assert eventos[0]["slot_end"] == ws.strftime("%Y-%m-%dT%H:%M")

    def test_evento_posterior_a_janela_fica_de_fora(
        self, organization_a: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_a, started_at=we + timedelta(hours=2))
        assert compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        ) == []

    def test_evento_pontual_no_inicio_da_janela_entra(
        self, organization_a: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_a, started_at=ws)
        eventos = compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        )
        assert len(eventos) == 1
        assert eventos[0]["pontual"] is True

    def test_evento_de_outra_org_nao_aparece(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        ws, we = self._janela()
        _evento(organization_b, titulo="Evento da B",
                started_at=ws + timedelta(hours=2))
        assert compute_atendimento_eventos_rede(
            organization_a, window_start=ws, window_end=we
        ) == []

    def test_slots_casam_com_os_do_grafico(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        d = compute_atendimento_horario(organization_a, days=7, foco="todos")
        inicio = d["window_start"] + timedelta(hours=10)
        _evento(organization_a, started_at=inicio,
                ended_at=inicio + timedelta(hours=3))
        eventos = compute_atendimento_eventos_rede(
            organization_a,
            window_start=d["window_start"],
            window_end=d["window_end"],
        )
        assert eventos[0]["slot_start"] in d["slots"]
        assert eventos[0]["slot_end"] in d["slots"]


def _fig(eventos: list[dict[str, Any]]) -> dict[str, Any]:
    d = {
        "labels": ["03/08 13h", "03/08 14h", "03/08 15h"],
        "slots": [
            "2026-08-03T13:00", "2026-08-03T14:00", "2026-08-03T15:00",
        ],
        "actual": [3, 37, 5],
        "expected": [3.0, 9.0, 4.0],
        "upper": [6.0, 15.0, 8.0],
        "lower": [0.0, 3.0, 1.0],
        "anomaly_x": [],
        "anomaly_y": [],
        "anomaly_slots": [],
        "detect": "spike",
        "billing_day_labels": [],
        "vencimentos": [],
    }
    return json.loads(charts.atendimento_horario_sazonal(d, eventos))


class TestShapesNoPlotly:
    """Overlay: faixa quando tem fim, linha tracejada quando é pontual."""

    def test_sem_eventos_nao_gera_shape_nem_anotacao(self) -> None:
        fig = _fig([])
        assert fig["layout"].get("shapes", []) == []
        assert fig["layout"].get("annotations", []) == []

    def test_evento_com_fim_vira_faixa(self) -> None:
        fig = _fig([
            {
                "tipo": "ROMPIMENTO", "tipo_label": "Rompimento",
                "titulo": "Rompimento troncal", "descricao": "",
                "cor": "#dc2626", "pontual": False,
                "slot_start": "2026-08-03T13:00", "slot_end": "2026-08-03T15:00",
                "started_at_str": "03/08/2026 13:10", "ended_at_str": "03/08/2026 15:20",
            }
        ])
        shapes = fig["layout"]["shapes"]
        assert len(shapes) == 1
        assert shapes[0]["type"] == "rect"
        # Posicionado pelos RÓTULOS do eixo categórico, não por datetime.
        assert shapes[0]["x0"] == "03/08 13h"
        assert shapes[0]["x1"] == "03/08 15h"
        assert "220,38,38" in shapes[0]["fillcolor"]  # cor do tipo (#dc2626)
        anot = fig["layout"]["annotations"]
        assert anot[0]["text"] == "Rompimento troncal"
        assert anot[0]["x"] == "03/08 13h"

    def test_evento_pontual_vira_linha_tracejada(self) -> None:
        fig = _fig([
            {
                "tipo": "QUEDA_LINK", "tipo_label": "Queda de link",
                "titulo": "Queda do link principal", "descricao": "",
                "cor": "#f59e0b", "pontual": True,
                "slot_start": "2026-08-03T14:00", "slot_end": "2026-08-03T14:00",
                "started_at_str": "03/08/2026 14:05", "ended_at_str": "",
            }
        ])
        shapes = fig["layout"]["shapes"]
        assert len(shapes) == 1
        assert shapes[0]["type"] == "line"
        assert shapes[0]["x0"] == shapes[0]["x1"] == "03/08 14h"
        assert shapes[0]["line"]["dash"] == "dash"
        assert shapes[0]["line"]["color"] == "#f59e0b"

    def test_evento_dentro_da_mesma_hora_vira_linha(self) -> None:
        """Faixa de largura zero seria invisível no eixo categórico."""
        fig = _fig([
            {
                "tipo": "MANUTENCAO", "tipo_label": "Manutenção programada",
                "titulo": "Troca de SFP", "descricao": "",
                "cor": "#2563eb", "pontual": False,
                "slot_start": "2026-08-03T14:00", "slot_end": "2026-08-03T14:00",
                "started_at_str": "03/08/2026 14:05", "ended_at_str": "03/08/2026 14:35",
            }
        ])
        assert fig["layout"]["shapes"][0]["type"] == "line"

    def test_slot_fora_dos_labels_e_ignorado(self) -> None:
        fig = _fig([
            {
                "tipo": "OUTRO", "tipo_label": "Outro", "titulo": "Fantasma",
                "descricao": "", "cor": "#7c3aed", "pontual": True,
                "slot_start": "2026-08-01T09:00", "slot_end": "2026-08-01T09:00",
                "started_at_str": "", "ended_at_str": "",
            }
        ])
        assert fig["layout"].get("shapes", []) == []


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCrudPelaPagina:
    def test_owner_cria_evento(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        inicio = _hour()
        resp = client.post(NOVO_URL, {
            "tipo": EventoRede.Tipo.ROMPIMENTO.value,
            "titulo": "Rompimento na troncal",
            "descricao": "Fibra rompida por obra",
            "started_at": _dt_param(inicio),
            "ended_at": _dt_param(inicio + timedelta(hours=3)),
            "q": "periodo=7d&foco=rede",
        })
        assert resp.status_code == 302
        assert resp["Location"].startswith(f"{TENDENCIAS_URL}?periodo=7d&foco=rede")
        set_current_organization(organization_a)
        ev = _qs(organization_a).get()
        assert ev.titulo == "Rompimento na troncal"
        assert ev.created_by == user_a
        assert ev.is_pontual is False

    def test_owner_cria_evento_pontual(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        client.post(NOVO_URL, {
            "tipo": EventoRede.Tipo.QUEDA_LINK.value,
            "titulo": "Queda do link",
            "started_at": _dt_param(_hour()),
            "ended_at": "",
        })
        assert _qs(organization_a).get().is_pontual is True

    def test_fim_antes_do_inicio_nao_salva(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        inicio = _hour()
        resp = client.post(NOVO_URL, {
            "tipo": EventoRede.Tipo.OUTRO.value,
            "titulo": "Inconsistente",
            "started_at": _dt_param(inicio),
            "ended_at": _dt_param(inicio - timedelta(hours=1)),
        })
        assert resp.status_code == 302
        assert "evento_erro=1" in resp["Location"]
        assert _qs(organization_a).count() == 0

    def test_sem_titulo_nao_salva(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.post(NOVO_URL, {
            "tipo": EventoRede.Tipo.OUTRO.value,
            "titulo": "   ",
            "started_at": _dt_param(_hour()),
        })
        assert "evento_erro=1" in resp["Location"]
        assert _qs(organization_a).count() == 0

    def test_owner_edita_evento(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        ev = _evento(organization_a)
        client.force_login(user_a)
        novo_inicio = _hour(days_ago=1, hour=9)
        resp = client.post(f"/operations/eventos-rede/{ev.id}/", {
            "action": "update",
            "tipo": EventoRede.Tipo.MANUTENCAO.value,
            "titulo": "Manutenção programada OLT",
            "descricao": "Janela noturna",
            "started_at": _dt_param(novo_inicio),
            "ended_at": _dt_param(novo_inicio + timedelta(hours=2)),
        })
        assert resp.status_code == 302
        ev.refresh_from_db()
        assert ev.titulo == "Manutenção programada OLT"
        assert ev.tipo == EventoRede.Tipo.MANUTENCAO.value
        assert ev.ended_at is not None

    def test_owner_exclui_evento(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        ev = _evento(organization_a)
        client.force_login(user_a)
        resp = client.post(f"/operations/eventos-rede/{ev.id}/", {"action": "delete"})
        assert resp.status_code == 302
        assert _qs(organization_a).filter(pk=ev.id).count() == 0

    def test_nao_owner_recebe_403_no_post(
        self, client: Any, organization_a: Organization
    ) -> None:
        membro = _member(organization_a, email="membro@a.test")
        client.force_login(membro)
        resp = client.post(NOVO_URL, {
            "titulo": "Não pode", "tipo": EventoRede.Tipo.OUTRO.value,
            "started_at": _dt_param(_hour()),
        })
        assert resp.status_code == 403
        assert _qs(organization_a).count() == 0

    def test_nao_owner_recebe_403_ao_excluir(
        self, client: Any, organization_a: Organization
    ) -> None:
        ev = _evento(organization_a)
        membro = _member(organization_a, email="membro2@a.test")
        client.force_login(membro)
        resp = client.post(
            f"/operations/eventos-rede/{ev.id}/", {"action": "delete"}
        )
        assert resp.status_code == 403
        assert _qs(organization_a).filter(pk=ev.id).exists()

    def test_owner_nao_edita_evento_de_outra_org(
        self, client: Any, user_a: User, organization_b: Organization
    ) -> None:
        ev = _evento(organization_b, titulo="Da org B")
        client.force_login(user_a)
        resp = client.post(f"/operations/eventos-rede/{ev.id}/", {
            "action": "update", "titulo": "Invadido",
            "tipo": EventoRede.Tipo.OUTRO.value,
            "started_at": _dt_param(_hour()),
        })
        assert resp.status_code == 404
        ev.refresh_from_db()
        assert ev.titulo == "Da org B"

    def test_owner_nao_exclui_evento_de_outra_org(
        self, client: Any, user_a: User, organization_b: Organization
    ) -> None:
        ev = _evento(organization_b)
        client.force_login(user_a)
        resp = client.post(
            f"/operations/eventos-rede/{ev.id}/", {"action": "delete"}
        )
        assert resp.status_code == 404
        assert _qs(organization_b).filter(pk=ev.id).exists()

    def test_get_nao_e_aceito(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        assert client.get(NOVO_URL).status_code == 405


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPaginaDeTendencias:
    def test_owner_ve_form_e_lista(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _evento(organization_a, titulo="Rompimento centro",
                started_at=_hour(), ended_at=_hour() + timedelta(hours=2))
        client.force_login(user_a)
        html = client.get(f"{TENDENCIAS_URL}?periodo=7d&foco=todos").content.decode()
        assert "Registrar evento" in html
        assert "Eventos no período" in html
        assert "Rompimento centro" in html
        assert NOVO_URL in html
        assert "excluir" in html

    def test_shape_do_evento_chega_no_json_do_grafico(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        inicio = _hour(days_ago=1, hour=10)
        _evento(organization_a, titulo="Queda de link",
                tipo=EventoRede.Tipo.QUEDA_LINK.value,
                started_at=inicio, ended_at=inicio + timedelta(hours=2))
        client.force_login(user_a)
        resp = client.get(f"{TENDENCIAS_URL}?periodo=7d&foco=todos")
        fig = json.loads(resp.context["horario_json"])
        rects = [s for s in fig["layout"]["shapes"] if s["type"] == "rect"]
        assert any(s["x0"] == inicio.strftime("%d/%m %Hh") for s in rects)
        assert [a["text"] for a in fig["layout"]["annotations"]] == ["Queda de link"]

    def test_nao_owner_nao_ve_botoes(
        self, client: Any, organization_a: Organization
    ) -> None:
        _evento(organization_a, titulo="Rompimento centro")
        membro = _member(organization_a, email="membro3@a.test")
        membro.get_active_membership()
        client.force_login(membro)
        html = client.get(f"{TENDENCIAS_URL}?periodo=7d&foco=todos").content.decode()
        assert "Registrar evento" not in html
        assert NOVO_URL not in html
        # A lista em si continua visível (leitura).
        assert "Eventos no período" in html
