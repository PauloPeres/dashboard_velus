"""Navegação dia ↔ hora da lista de atendimentos (#88).

A lista genérica (#87) já entendia `?d=`, mas não dava como chegar lá: da hora
só se andava hora a hora. Aqui se testa o caminho:

- **hora → dia**: botão "Ver o dia todo" preservando foco, departamento e o
  período de origem, e levando a hora de origem em `origem_h`;
- **dia → hora**: breadcrumb de volta e mini-gráfico por hora, ambos sem perder
  período/filtros;
- **paridade**: total do dia == soma das 24 horas do MESMO recorte (o requisito
  mais sensível da série — a tela do dia não pode contar diferente da hora);
- **sem futuro**: o dia seguinte ao corrente não é navegável.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_atendimento_lista,
    compute_atendimento_lista_por_hora,
)
from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

_SP = ZoneInfo("America/Sao_Paulo")
URL = "/operations/atendimento-lista/"
_H_FMT = "%Y-%m-%dT%H:%M"


def _dia_base(days_ago: int = 2) -> datetime:
    """Meia-noite local de um dia inteiramente no passado (24 horas fechadas)."""
    local = timezone.localtime(timezone.now(), _SP)
    return (local - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
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
    customer_name: str = "",
    motivos: list[str] | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
        departamento=departamento,
        customer_name=customer_name,
        atendente_nome="Ana",
        protocol=external_id,
        tags=[],
        motivos=motivos or [],
    )


def _semeia_dia(org: Organization, dia: datetime) -> dict[int, int]:
    """Atendimentos espalhados pelo dia, incluindo as bordas de meia-noite.

    Devolve `{hora: quantidade}` esperado. Os vizinhos (23:59 da véspera e
    00:00 do dia seguinte) entram como ruído — não podem contar aqui.
    """
    esperado = {0: 2, 9: 3, 14: 1, 23: 4}
    for hora, n in esperado.items():
        for i in range(n):
            _at(
                org,
                external_id=f"d{hora:02d}-{i}",
                opened_at=dia + timedelta(hours=hora, minutes=i),
                customer_name=f"Cliente {hora:02d}{i}",
            )
    _at(org, external_id="vespera", opened_at=dia - timedelta(seconds=1))
    _at(org, external_id="seguinte", opened_at=dia + timedelta(days=1))
    return esperado


# =============================================================================
# Agregação — paridade dia vs soma das horas
# =============================================================================
@pytest.mark.django_db
class TestParidadeDiaHoras:
    def test_total_do_dia_bate_com_a_soma_das_24_horas(
        self, organization_a: Organization
    ) -> None:
        """O requisito central: nenhum atendimento some nem conta duas vezes."""
        set_current_organization(organization_a)
        dia = _dia_base()
        esperado = _semeia_dia(organization_a, dia)

        do_dia = compute_atendimento_lista(
            organization_a, start=dia, end=dia + timedelta(days=1), foco="todos"
        )
        soma_horas = 0
        for h in range(24):
            inicio = dia + timedelta(hours=h)
            da_hora = compute_atendimento_lista(
                organization_a,
                start=inicio,
                end=inicio + timedelta(hours=1),
                foco="todos",
            )
            assert da_hora["total"] == esperado.get(h, 0)
            soma_horas += da_hora["total"]

        assert do_dia["total"] == soma_horas == sum(esperado.values())

    def test_mini_grafico_soma_o_total_do_dia(
        self, organization_a: Organization
    ) -> None:
        """As barras saem do mesmo queryset do total — a soma tem que fechar."""
        set_current_organization(organization_a)
        dia = _dia_base()
        esperado = _semeia_dia(organization_a, dia)

        slots = compute_atendimento_lista_por_hora(
            organization_a, start=dia, end=dia + timedelta(days=1), foco="todos"
        )
        do_dia = compute_atendimento_lista(
            organization_a, start=dia, end=dia + timedelta(days=1), foco="todos"
        )
        assert len(slots) == 24  # hora vazia vira zero, não some
        assert sum(s["count"] for s in slots) == do_dia["total"]
        assert {s["label"] for s in slots} == {f"{h:02d}h" for h in range(24)}
        for h, n in esperado.items():
            assert slots[h]["count"] == n
            assert slots[h]["param"] == (dia + timedelta(hours=h)).strftime(_H_FMT)

    def test_mini_grafico_respeita_o_recorte(
        self, organization_a: Organization
    ) -> None:
        """Foco/departamento valem no gráfico igual valem no total da tela."""
        set_current_organization(organization_a)
        dia = _dia_base()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        _at(organization_a, external_id="s1", opened_at=dia + timedelta(hours=10),
            departamento=sup)
        _at(organization_a, external_id="x1", opened_at=dia + timedelta(hours=10))

        slots = compute_atendimento_lista_por_hora(
            organization_a, start=dia, end=dia + timedelta(days=1), foco="suporte"
        )
        do_dia = compute_atendimento_lista(
            organization_a, start=dia, end=dia + timedelta(days=1), foco="suporte"
        )
        assert sum(s["count"] for s in slots) == do_dia["total"] == 1
        assert slots[10]["count"] == 1

    def test_isolamento_por_organizacao(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        dia = _dia_base()
        set_current_organization(organization_a)
        _at(organization_a, external_id="a1", opened_at=dia + timedelta(hours=8))
        set_current_organization(organization_b)
        _at(organization_b, external_id="b1", opened_at=dia + timedelta(hours=8))
        _at(organization_b, external_id="b2", opened_at=dia + timedelta(hours=9))

        set_current_organization(organization_a)
        slots_a = compute_atendimento_lista_por_hora(
            organization_a, start=dia, end=dia + timedelta(days=1)
        )
        assert sum(s["count"] for s in slots_a) == 1


# =============================================================================
# View — navegação hora → dia → hora
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestNavegacaoHoraDia:
    def test_hora_oferece_ver_o_dia_todo_com_os_filtros(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        dia = _dia_base()
        h = dia + timedelta(hours=14)
        tri = _departamento(organization_a, external_id="dtri", nome="Triagem")
        _at(organization_a, external_id="a1", opened_at=h, departamento=tri)

        client.force_login(user_a)
        html = client.get(
            f"{URL}?h={h.strftime(_H_FMT)}&foco=todos&departamento={tri.id}&periodo=7d"
        ).content.decode()

        assert "Ver o dia todo" in html
        assert f"d={dia.date().isoformat()}" in html
        assert f"departamento={tri.id}" in html
        assert "foco=todos" in html
        assert "periodo=7d" in html
        # A hora de origem viaja pro dia saber o caminho de volta.
        assert f"origem_h={h.strftime(_H_FMT)}" in html

    def test_dia_volta_para_a_hora_de_origem_sem_perder_nada(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        dia = _dia_base()
        h = dia + timedelta(hours=14)
        tri = _departamento(organization_a, external_id="dtri", nome="Triagem")
        _at(organization_a, external_id="a1", opened_at=h, departamento=tri)

        client.force_login(user_a)
        resp = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=todos&departamento={tri.id}"
            f"&periodo=7d&origem_h={h.strftime(_H_FMT)}"
        )
        html = resp.content.decode()
        assert resp.status_code == 200
        assert f"Dia {dia.strftime('%d/%m/%Y')}" in html
        assert "voltar para 14h" in html
        assert f"h={h.strftime(_H_FMT)}" in html

        # E o link de volta realmente reabre a hora com os mesmos filtros.
        volta = client.get(
            f"{URL}?h={h.strftime(_H_FMT)}&foco=todos&departamento={tri.id}&periodo=7d"
        )
        assert volta.status_code == 200
        assert "1 atendimento entre" in volta.content.decode()

    def test_origem_h_de_outro_dia_e_ignorada(
        self, client: Any, user_a: User
    ) -> None:
        """Breadcrumb só faz sentido pra uma hora DESTE dia — o resto some."""
        dia = _dia_base()
        alheia = (dia - timedelta(days=3) + timedelta(hours=9)).strftime(_H_FMT)
        client.force_login(user_a)
        html = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=todos&origem_h={alheia}"
        ).content.decode()
        assert "voltar para" not in html

        html_invalida = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=todos&origem_h=nao-e-hora"
        ).content.decode()
        assert "voltar para" not in html_invalida

    def test_navegacao_dia_anterior_e_seguinte(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        dia = _dia_base(days_ago=3)
        _at(organization_a, external_id="a1", opened_at=dia + timedelta(hours=8))

        client.force_login(user_a)
        html = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=suporte&periodo=7d"
        ).content.decode()

        anterior = (dia - timedelta(days=1)).date().isoformat()
        seguinte = (dia + timedelta(days=1)).date().isoformat()
        assert f"?d={anterior}&amp;foco=suporte" in html
        assert f"?d={seguinte}&amp;foco=suporte" in html
        assert (dia - timedelta(days=1)).strftime("%d/%m") in html

    def test_nao_navega_para_o_futuro(self, client: Any, user_a: User) -> None:
        """O dia corrente é parcial mas navegável; o seguinte não existe."""
        hoje = _dia_base(days_ago=0)
        amanha = (hoje + timedelta(days=1)).date().isoformat()
        client.force_login(user_a)
        html = client.get(
            f"{URL}?d={hoje.date().isoformat()}&foco=todos&periodo=7d"
        ).content.decode()
        assert f"?d={amanha}" not in html
        assert f"?d={(hoje - timedelta(days=1)).date().isoformat()}" in html

    def test_paginacao_do_dia_preserva_o_caminho_de_volta(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        dia = _dia_base()
        h = dia + timedelta(hours=14)
        Atendimento.objects.bulk_create([
            Atendimento(
                organization=organization_a,
                source_type=SourceType.OPA.value,
                external_id=f"bulk-{i}",
                status=Atendimento.Status.CLOSED.value,
                opened_at=h + timedelta(seconds=i),
                customer_name=f"Cliente {i:03d}",
                atendente_nome="Ana",
                protocol=f"P{i:03d}",
                tags=[],
                motivos=[],
            )
            for i in range(150)
        ])

        client.force_login(user_a)
        html = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=todos&periodo=7d"
            f"&origem_h={h.strftime(_H_FMT)}"
        ).content.decode()
        assert "página 1 de 2" in html
        assert f"origem_h={h.strftime(_H_FMT)}&page=2" in html

    def test_mini_grafico_do_dia_com_drilldown(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        dia = _dia_base()
        _semeia_dia(organization_a, dia)

        client.force_login(user_a)
        html = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=todos&periodo=7d"
        ).content.decode()
        assert 'id="horas-chart"' in html
        assert 'id="horas-data"' in html
        # O slot viaja no customdata (não no rótulo) — contrato do drill-down.
        assert (dia + timedelta(hours=9)).strftime(_H_FMT) in html
        assert "plotly_click" in html

    def test_hora_e_periodo_nao_tem_mini_grafico(
        self, client: Any, user_a: User
    ) -> None:
        client.force_login(user_a)
        h = (_dia_base() + timedelta(hours=14)).strftime(_H_FMT)
        assert 'id="horas-chart"' not in client.get(
            f"{URL}?h={h}&foco=todos"
        ).content.decode()
        assert 'id="horas-chart"' not in client.get(
            f"{URL}?periodo=7d&foco=todos"
        ).content.decode()

    def test_csv_do_dia_ignora_o_caminho_de_volta(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """`origem_h` é só navegação: não pode recortar o export pra uma hora."""
        set_current_organization(organization_a)
        dia = _dia_base()
        _semeia_dia(organization_a, dia)
        h = dia + timedelta(hours=14)

        client.force_login(user_a)
        resp = client.get(
            f"{URL}?d={dia.date().isoformat()}&foco=todos&periodo=7d"
            f"&origem_h={h.strftime(_H_FMT)}&format=csv"
        )
        assert resp.status_code == 200
        linhas = [ln for ln in resp.content.decode("utf-8-sig").split("\r\n") if ln]
        assert len(linhas) == 11  # header + 10 do dia (2+3+1+4)
        assert resp["Content-Disposition"].endswith(
            f'atendimentos_{dia.date().isoformat()}.csv"'
        )
