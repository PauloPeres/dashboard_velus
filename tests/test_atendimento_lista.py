"""Testes da lista genérica de atendimentos (#87).

A página atende três recortes — uma hora (`?h=`), um dia (`?d=`) e período +
departamento/motivo/tag — e é o destino dos drill-downs de atendimento. O que
importa aqui:

- **paridade de contagem**: o total tem que bater EXATAMENTE com o agregador do
  gráfico que originou o clique (por isso os testes comparam com
  `compute_atendimento_horario` / `compute_atendimento_tendencias`, não com
  números fixos);
- **paginação com total real** (a lista antiga truncava em 500 linhas em
  silêncio) e CSV do recorte inteiro, não da página;
- isolamento por organização e RBAC iguais aos da tela da hora.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_atendimento_horario,
    compute_atendimento_lista,
    compute_atendimento_tendencias,
)
from apps.atendimento.infrastructure.models import Atendimento, Departamento
from apps.customers.infrastructure.models import Customer
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import (
    AccessGroup,
    Organization,
    OrganizationMembership,
    User,
)

_SP = ZoneInfo("America/Sao_Paulo")
URL = "/operations/atendimento-lista/"


def _hour(days_ago: int = 2, hour: int = 14) -> datetime:
    """Hora cheia (local) alguns dias atrás — dentro da janela padrão da página."""
    local = timezone.localtime(timezone.now(), _SP)
    return (local - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _h_param(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _d_param(dt: datetime) -> str:
    return dt.date().isoformat()


def _departamento(org: Organization, *, external_id: str, nome: str) -> Departamento:
    return Departamento.objects.create(
        organization=org, source_type=SourceType.OPA.value,
        external_id=external_id, nome=nome,
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


def _at(
    org: Organization,
    *,
    external_id: str,
    opened_at: datetime,
    departamento: Departamento | None = None,
    customer: Customer | None = None,
    customer_name: str = "",
    customer_document: str = "",
    atendente_nome: str = "",
    protocol: str = "",
    tags: list[str] | None = None,
    motivos: list[str] | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        opened_at=opened_at,
        departamento=departamento,
        customer=customer,
        customer_name=customer_name,
        customer_document=customer_document,
        atendente_nome=atendente_nome,
        protocol=protocol,
        tags=tags or [],
        motivos=motivos or [],
    )


def _muitos(
    org: Organization, *, base: datetime, n: int, departamento: Departamento | None
) -> None:
    """N atendimentos na mesma hora — pra exercitar a paginação de verdade."""
    Atendimento.objects.bulk_create([
        Atendimento(
            organization=org,
            source_type=SourceType.OPA.value,
            external_id=f"bulk-{i}",
            status=Atendimento.Status.CLOSED.value,
            opened_at=base + timedelta(seconds=i),
            departamento=departamento,
            customer_name=f"Cliente {i:03d}",
            atendente_nome="Ana",
            protocol=f"P{i:03d}",
            tags=[],
            motivos=[],
        )
        for i in range(n)
    ])


def _dia(base: datetime) -> tuple[datetime, datetime]:
    """Janela `[00:00, 00:00 do dia seguinte)` do dia de `base`."""
    start = base.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


# =============================================================================
# Agregação — os três recortes e a paridade com o gráfico de origem
# =============================================================================
@pytest.mark.django_db
class TestComputeAtendimentoLista:
    def test_recorte_de_hora_bate_com_o_ponto_do_grafico(
        self, organization_a: Organization
    ) -> None:
        """Total da lista == valor da hora no gráfico horário (mesmo helper de foco)."""
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        for i in range(5):
            _at(organization_a, external_id=f"s{i}",
                opened_at=h + timedelta(minutes=5 * i), departamento=sup)
        # Ruído: fora da hora e fora do foco.
        _at(organization_a, external_id="fora", opened_at=h + timedelta(hours=3),
            departamento=sup)
        _at(organization_a, external_id="outro-foco", opened_at=h)

        grafico = compute_atendimento_horario(organization_a, days=7, foco="suporte")
        idx = grafico["labels"].index(h.strftime("%d/%m %Hh"))
        lista = compute_atendimento_lista(
            organization_a, start=h, end=h + timedelta(hours=1), foco="suporte"
        )
        assert lista["total"] == grafico["actual"][idx] == 5

    def test_recorte_de_dia_pega_o_dia_inteiro(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        start, end = _dia(h)
        _at(organization_a, external_id="00h", opened_at=start)
        _at(organization_a, external_id="14h", opened_at=h)
        _at(organization_a, external_id="2359",
            opened_at=end - timedelta(minutes=1))
        # Vizinhos: véspera e dia seguinte ficam de fora.
        _at(organization_a, external_id="antes", opened_at=start - timedelta(seconds=1))
        _at(organization_a, external_id="depois", opened_at=end)

        data = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos"
        )
        assert data["total"] == 3
        assert data["resumo"]["total"] == 3

    def test_periodo_mais_departamento_bate_com_o_grafico(
        self, organization_a: Organization
    ) -> None:
        """Departamento + janela: o total é o mesmo que o gráfico horário soma."""
        set_current_organization(organization_a)
        h = _hour()
        tri = _departamento(organization_a, external_id="dtri", nome="Triagem")
        outro = _departamento(organization_a, external_id="dout", nome="Financeiro")
        for i in range(4):
            _at(organization_a, external_id=f"t{i}",
                opened_at=h - timedelta(days=i), departamento=tri)
        _at(organization_a, external_id="x1", opened_at=h, departamento=outro)

        start = (h - timedelta(days=6)).replace(hour=0, minute=0)
        end = h + timedelta(hours=1)
        grafico = compute_atendimento_horario(
            organization_a, start=start, end=h, foco="todos", departamento_id=tri.id
        )
        lista = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos", departamento_id=tri.id
        )
        assert lista["total"] == sum(grafico["actual"]) == 4
        assert lista["departamento_nome"] == "Triagem"

    def test_periodo_mais_motivo_bate_com_a_serie_de_tendencias(
        self, organization_a: Organization
    ) -> None:
        """Motivo + janela: o total é o mesmo da série destacada em Tendências."""
        set_current_organization(organization_a)
        h = _hour()
        for i in range(3):
            _at(organization_a, external_id=f"m{i}",
                opened_at=h - timedelta(days=i), motivos=["Lentidão"])
        _at(organization_a, external_id="outro", opened_at=h, motivos=["Financeiro"])
        _at(organization_a, external_id="tag-only", opened_at=h, tags=["Lentidão"])

        start = (h - timedelta(days=6)).replace(hour=0, minute=0)
        end = h + timedelta(hours=1)
        tendencias = compute_atendimento_tendencias(
            organization_a, start=start, end=end, focus_motivo="Lentidão"
        )
        lista = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos", motivo="Lentidão"
        )
        assert lista["total"] == sum(tendencias["motivo_focus"]["values"]) == 3

    def test_recorte_por_tag(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="t1", opened_at=h, tags=["LOS"])
        _at(organization_a, external_id="t2", opened_at=h, tags=["LOS", "Quedas"])
        _at(organization_a, external_id="t3", opened_at=h, motivos=["LOS"])

        start, end = _dia(h)
        data = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos", tag="LOS"
        )
        assert data["total"] == 2

    def test_paginacao_mostra_total_real(self, organization_a: Organization) -> None:
        """O total é o do recorte, não o da página — e o resumo também."""
        set_current_organization(organization_a)
        h = _hour()
        _muitos(organization_a, base=h, n=250, departamento=None)
        start, end = _dia(h)

        p1 = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos", page=1, per_page=100
        )
        p3 = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos", page=3, per_page=100
        )
        assert p1["total"] == p3["total"] == 250
        assert len(p1["rows"]) == 100
        assert len(p3["rows"]) == 50
        assert p1["num_pages"] == p3["num_pages"] == 3
        assert (p1["has_prev"], p1["has_next"]) == (False, True)
        assert (p3["has_prev"], p3["has_next"]) == (True, False)
        assert (p1["page_first"], p1["page_last"]) == (1, 100)
        assert (p3["page_first"], p3["page_last"]) == (201, 250)
        # Resumo é do recorte inteiro, não da página exibida.
        assert p1["resumo"]["total"] == 250
        assert p1["resumo"]["atendentes"][0]["count"] == 250
        # Sem sobreposição entre páginas.
        assert not {r["atendimento_id"] for r in p1["rows"]} & {
            r["atendimento_id"] for r in p3["rows"]
        }

    def test_pagina_fora_do_intervalo_cai_na_ultima(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _muitos(organization_a, base=h, n=120, departamento=None)
        start, end = _dia(h)
        data = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos", page=99, per_page=100
        )
        assert data["page"] == 2
        assert len(data["rows"]) == 20

    def test_isolamento_por_organizacao(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        h = _hour()
        start, end = _dia(h)
        set_current_organization(organization_a)
        _at(organization_a, external_id="a1", opened_at=h, customer_name="Cliente A")
        set_current_organization(organization_b)
        _at(organization_b, external_id="b1", opened_at=h, customer_name="Cliente B")
        _at(organization_b, external_id="b2", opened_at=h, customer_name="Outro B")

        set_current_organization(organization_a)
        data_a = compute_atendimento_lista(
            organization_a, start=start, end=end, foco="todos"
        )
        assert data_a["total"] == 1
        assert data_a["rows"][0]["customer_name"] == "Cliente A"

        set_current_organization(organization_b)
        data_b = compute_atendimento_lista(
            organization_b, start=start, end=end, foco="todos"
        )
        assert data_b["total"] == 2
        assert "Cliente A" not in [r["customer_name"] for r in data_b["rows"]]


# =============================================================================
# View — recortes, paginação, CSV, RBAC
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoListaView:
    def test_recorte_de_hora(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="a1", opened_at=h,
            customer_name="Fulano de Tal", atendente_nome="Ana",
            protocol="2026080312345", tags=["Sem Conexão"])
        client.force_login(user_a)
        resp = client.get(f"{URL}?h={_h_param(h)}&foco=todos")

        assert resp.status_code == 200
        html = resp.content.decode()
        assert "Fulano de Tal" in html
        assert "2026080312345" in html
        assert "1 atendimento entre" in html
        assert h.strftime("%d/%m/%Y") in html
        assert "Exportar CSV" in html

    def test_recorte_de_dia(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        start, end = _dia(h)
        _at(organization_a, external_id="cedo", opened_at=start,
            customer_name="Cliente Cedo")
        _at(organization_a, external_id="tarde", opened_at=h,
            customer_name="Cliente Tarde")
        _at(organization_a, external_id="amanha", opened_at=end,
            customer_name="Cliente Amanha")

        client.force_login(user_a)
        html = client.get(f"{URL}?d={_d_param(h)}&foco=todos").content.decode()
        assert f"2 atendimentos em {h.strftime('%d/%m/%Y')}" in html
        assert "Cliente Cedo" in html
        assert "Cliente Tarde" in html
        assert "Cliente Amanha" not in html

    def test_recorte_de_periodo_com_departamento(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        tri = _departamento(organization_a, external_id="dtri", nome="Triagem")
        _at(organization_a, external_id="t1", opened_at=h, departamento=tri,
            customer_name="Da Triagem")
        _at(organization_a, external_id="x1", opened_at=h,
            customer_name="Sem Departamento")

        client.force_login(user_a)
        resp = client.get(f"{URL}?periodo=7d&foco=todos&departamento={tri.id}")
        html = resp.content.decode()
        assert resp.status_code == 200
        assert "1 atendimento no período" in html
        assert "Da Triagem" in html
        assert "Sem Departamento" not in html
        assert "Triagem" in html  # descrição do recorte
        # No recorte de período o badge/barra do componente (#86) aparecem.
        assert "Últimos 7 dias" in html

    def test_recorte_de_periodo_com_motivo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="m1", opened_at=h, motivos=["Lentidão"],
            customer_name="Com Lentidao")
        _at(organization_a, external_id="m2", opened_at=h, motivos=["Financeiro"],
            customer_name="Com Financeiro")

        client.force_login(user_a)
        html = client.get(
            f"{URL}?periodo=7d&foco=todos&motivo=Lentid%C3%A3o"
        ).content.decode()
        assert "1 atendimento no período" in html
        assert "Com Lentidao" in html
        assert "Com Financeiro" not in html

    def test_hora_nao_mostra_barra_de_periodo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Em hora/dia a janela é o recorte — o período só viaja pro voltar."""
        client.force_login(user_a)
        html = client.get(
            f"{URL}?h={_h_param(_hour())}&foco=todos&periodo=7d"
        ).content.decode()
        assert "Personalizado:" not in html
        assert "Voltar para Tendências de Atendimento" in html
        assert "periodo=7d" in html

    def test_paginacao_na_tela(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _muitos(organization_a, base=h, n=150, departamento=None)

        client.force_login(user_a)
        p1 = client.get(f"{URL}?d={_d_param(h)}&foco=todos").content.decode()
        assert "150 atendimentos em" in p1  # total REAL, não o da página
        assert "Mostrando 1–100 de 150" in p1
        assert "página 1 de 2" in p1
        assert "Cliente 000" in p1
        assert "Cliente 149" not in p1

        p2 = client.get(f"{URL}?d={_d_param(h)}&foco=todos&page=2").content.decode()
        assert "Mostrando 101–150 de 150" in p2
        assert "Cliente 149" in p2
        assert "Cliente 000" not in p2

    def test_h_invalido_volta_para_a_origem(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?h=nao-e-data&periodo=7d&foco=rede")
        assert resp.status_code == 302
        assert resp.url.startswith("/operations/atendimento-tendencias/?")
        assert "periodo=7d" in resp.url
        assert "foco=rede" in resp.url

    def test_d_invalido_volta_para_a_origem(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?d=2026-13-45&periodo=7d")
        assert resp.status_code == 302
        assert resp.url.startswith("/operations/atendimento-tendencias/?")

    def test_origem_escolhe_o_link_de_voltar(
        self, client: Any, user_a: User
    ) -> None:
        """`origem` é whitelist de rota — nada de URL crua na querystring."""
        client.force_login(user_a)
        html = client.get(f"{URL}?periodo=7d&origem=atendimento").content.decode()
        assert 'href="/operations/atendimento/?' in html

        html_invalida = client.get(f"{URL}?periodo=7d&origem=http://evil").content.decode()
        assert "evil" not in html_invalida
        assert 'href="/operations/atendimento-tendencias/?' in html_invalida

    def test_isolamento_cross_tenant_na_view(
        self,
        client: Any,
        user_b: User,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        h = _hour()
        set_current_organization(organization_a)
        _at(organization_a, external_id="a1", opened_at=h, customer_name="Cliente ACME")
        set_current_organization(organization_b)
        _at(organization_b, external_id="b1", opened_at=h, customer_name="Cliente Brava")

        client.force_login(user_b)
        html = client.get(f"{URL}?d={_d_param(h)}&foco=todos").content.decode()
        assert "Cliente Brava" in html
        assert "Cliente ACME" not in html

    def test_sem_acesso_a_aba_de_tendencias(
        self, client: Any, organization_a: Organization
    ) -> None:
        user = _membro_do_grupo(
            organization_a, email="sem-acesso@acme.test", pages=["executive"]
        )
        client.force_login(user)
        resp = client.get(f"{URL}?h={_h_param(_hour())}")
        assert resp.status_code in (302, 403)
        if resp.status_code == 302:
            assert not resp.url.startswith(URL)

    def test_acesso_herdado_da_aba_de_tendencias(
        self, client: Any, organization_a: Organization
    ) -> None:
        user = _membro_do_grupo(
            organization_a, email="com-acesso@acme.test",
            pages=["atendimento_tendencias"],
        )
        client.force_login(user)
        assert client.get(f"{URL}?h={_h_param(_hour())}").status_code == 200


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoListaLinksPorRbac:
    """Links da tabela apontam pra abas de OUTRAS chaves (customers /
    conversas_ruins) — só renderizam pra quem tem acesso a elas."""

    def _seed(self, org: Organization) -> datetime:
        set_current_organization(org)
        h = _hour()
        customer = Customer.objects.create(
            organization=org, source_type="IXC", external_id="ixc-1",
            document="12345678901", name="Fulano de Tal",
            status=Customer.Status.ACTIVE.value,
        )
        _at(org, external_id="a1", opened_at=h, customer=customer,
            customer_name="Fulano de Tal", protocol="P1")
        return h

    def test_grupo_so_com_tendencias_nao_ve_links(
        self, client: Any, organization_a: Organization
    ) -> None:
        h = self._seed(organization_a)
        user = _membro_do_grupo(
            organization_a, email="restrito@acme.test",
            pages=["atendimento_tendencias"],
        )
        client.force_login(user)
        resp = client.get(f"{URL}?h={_h_param(h)}&foco=todos")
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "/customers/" not in html
        assert "/operations/conversas-ruins/" not in html
        # Texto continua visível, só não é link.
        assert "Fulano de Tal" in html
        assert "P1" in html

    def test_acesso_total_ve_links(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        h = self._seed(organization_a)
        client.force_login(user_a)
        html = client.get(f"{URL}?h={_h_param(h)}&foco=todos").content.decode()
        assert 'href="/customers/' in html
        assert 'href="/operations/conversas-ruins/' in html


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoListaCsv:
    def test_csv_header_linhas_e_bom(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        _at(organization_a, external_id="a1", opened_at=h, departamento=sup,
            customer_name="Cliente Ação", customer_document="12345678901",
            atendente_nome="José", protocol="P1", motivos=["Lentidão"],
            tags=["Quedas"])
        _at(organization_a, external_id="a2", opened_at=h + timedelta(minutes=30),
            departamento=sup, customer_name="Outro Cliente", protocol="P2")

        client.force_login(user_a)
        resp = client.get(f"{URL}?h={_h_param(h)}&foco=suporte&format=csv")

        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("text/csv")
        filename = f"atendimentos_{h.strftime('%Y-%m-%d_%Hh')}.csv"
        assert resp["Content-Disposition"] == f'attachment; filename="{filename}"'

        raw = resp.content
        assert raw.startswith(b"\xef\xbb\xbf")  # BOM UTF-8
        linhas = [ln for ln in raw.decode("utf-8-sig").split("\r\n") if ln]
        assert linhas[0] == (
            "Cliente;Documento;Horário;Atendente;Departamento;"
            "Categorias;Protocolo;Status"
        )
        assert len(linhas) == 3  # header + 2 atendimentos
        assert linhas[1].startswith("Cliente Ação;12345678901;")
        assert "Lentidão, Quedas" in linhas[1]
        assert linhas[1].endswith(";P1;Finalizado")

    def test_csv_exporta_o_recorte_inteiro_nao_a_pagina(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _muitos(organization_a, base=h, n=150, departamento=None)

        client.force_login(user_a)
        resp = client.get(f"{URL}?d={_d_param(h)}&foco=todos&format=csv")
        linhas = [ln for ln in resp.content.decode("utf-8-sig").split("\r\n") if ln]
        assert len(linhas) == 151  # header + 150, além das 100 da página
        assert resp["Content-Disposition"].endswith(
            f'atendimentos_{_d_param(h)}.csv"'
        )

    def test_csv_respeita_o_recorte(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        com = _departamento(organization_a, external_id="dcom", nome="Comercial")
        _at(organization_a, external_id="s1", opened_at=h, departamento=sup,
            customer_name="Do Suporte")
        _at(organization_a, external_id="c1", opened_at=h, departamento=com,
            customer_name="Do Comercial")

        client.force_login(user_a)
        text = client.get(
            f"{URL}?d={_d_param(h)}&foco=comercial&format=csv"
        ).content.decode("utf-8-sig")
        assert "Do Comercial" in text
        assert "Do Suporte" not in text

    def test_csv_isolado_por_organizacao(
        self,
        client: Any,
        user_b: User,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        h = _hour()
        set_current_organization(organization_a)
        _at(organization_a, external_id="a1", opened_at=h, customer_name="Cliente ACME")
        set_current_organization(organization_b)
        _at(organization_b, external_id="b1", opened_at=h, customer_name="Cliente Brava")

        client.force_login(user_b)
        text = client.get(
            f"{URL}?d={_d_param(h)}&foco=todos&format=csv"
        ).content.decode("utf-8-sig")
        assert "Cliente Brava" in text
        assert "Cliente ACME" not in text
