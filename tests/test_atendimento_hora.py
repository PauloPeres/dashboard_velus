"""Testes da lista de atendimentos de uma hora do gráfico horário (#76).

Cobre `compute_atendimento_hora` (bordas da hora, recorte de foco, isolamento
por organização, coerência com o ponto do gráfico) e a view `atendimento_hora`
(render, RBAC herdado da aba de tendências, `?h=` inválido e export CSV).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_atendimento_hora,
    compute_atendimento_horario,
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
URL = "/operations/atendimento-hora/"


def _hour(days_ago: int = 2, hour: int = 14) -> datetime:
    """Hora cheia (local) alguns dias atrás — dentro da janela padrão da página."""
    local = timezone.localtime(timezone.now(), _SP)
    return (local - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def _h_param(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


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


@pytest.mark.django_db
class TestComputeAtendimentoHora:
    def test_bordas_da_hora(self, organization_a: Organization) -> None:
        """`opened_at == h` entra; `h + 1h` (e `h - 1s`) não."""
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="antes", opened_at=h - timedelta(seconds=1))
        _at(organization_a, external_id="inicio", opened_at=h)
        _at(organization_a, external_id="meio", opened_at=h + timedelta(minutes=59))
        _at(organization_a, external_id="depois", opened_at=h + timedelta(hours=1))

        data = compute_atendimento_hora(organization_a, hour_start=h, foco="todos")
        assert data["total"] == 2
        assert data["hour_start"] == h
        assert data["hour_end"] == h + timedelta(hours=1)
        ids = [r["atendimento_id"] for r in data["rows"]]
        assert len(ids) == 2
        # Ordenado por opened_at crescente.
        assert data["rows"][0]["opened_at"] < data["rows"][1]["opened_at"]

    def test_hora_nao_cheia_e_truncada(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="a1", opened_at=h + timedelta(minutes=10))
        data = compute_atendimento_hora(
            organization_a, hour_start=h + timedelta(minutes=42), foco="todos"
        )
        assert data["hour_start"] == h
        assert data["total"] == 1

    def test_foco_filtra_departamento(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        com = _departamento(organization_a, external_id="dcom", nome="Comercial")
        _at(organization_a, external_id="s1", opened_at=h, departamento=sup)
        _at(organization_a, external_id="c1", opened_at=h, departamento=com)

        suporte = compute_atendimento_hora(organization_a, hour_start=h, foco="suporte")
        comercial = compute_atendimento_hora(
            organization_a, hour_start=h, foco="comercial"
        )
        todos = compute_atendimento_hora(organization_a, hour_start=h, foco="todos")
        assert suporte["total"] == 1
        assert suporte["rows"][0]["departamento_nome"] == "Suporte"
        assert comercial["total"] == 1
        assert comercial["rows"][0]["departamento_nome"] == "Comercial"
        assert todos["total"] == 2

    def test_foco_rede_por_motivo_ou_tag(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(organization_a, external_id="r1", opened_at=h, tags=["Sem Conexão"])
        _at(organization_a, external_id="r2", opened_at=h, motivos=["Quedas"])
        _at(organization_a, external_id="x1", opened_at=h, motivos=["Financeiro"])

        data = compute_atendimento_hora(organization_a, hour_start=h, foco="rede")
        assert data["total"] == 2

    def test_departamento_id_so_vale_no_foco_todos(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        tri = _departamento(organization_a, external_id="dtri", nome="Triagem")
        _at(organization_a, external_id="s1", opened_at=h, departamento=sup)
        _at(organization_a, external_id="t1", opened_at=h, departamento=tri)

        data = compute_atendimento_hora(
            organization_a, hour_start=h, foco="todos", departamento_id=tri.id
        )
        assert data["total"] == 1
        assert data["departamento_nome"] == "Triagem"

    def test_bate_com_o_ponto_do_grafico(self, organization_a: Organization) -> None:
        """A contagem da lista == valor da hora correspondente no gráfico."""
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        for i in range(4):
            _at(
                organization_a,
                external_id=f"s{i}",
                opened_at=h + timedelta(minutes=5 * i),
                departamento=sup,
            )
        # Ruído fora da hora e fora do foco.
        _at(organization_a, external_id="fora", opened_at=h + timedelta(hours=3),
            departamento=sup)
        _at(organization_a, external_id="outro-foco", opened_at=h)

        grafico = compute_atendimento_horario(organization_a, days=7, foco="suporte")
        idx = grafico["labels"].index(h.strftime("%d/%m %Hh"))
        lista = compute_atendimento_hora(organization_a, hour_start=h, foco="suporte")
        assert lista["total"] == grafico["actual"][idx] == 4

    def test_isolamento_por_organizacao(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        h = _hour()
        set_current_organization(organization_a)
        _at(organization_a, external_id="a1", opened_at=h, customer_name="Cliente A")
        set_current_organization(organization_b)
        _at(organization_b, external_id="b1", opened_at=h, customer_name="Cliente B")

        set_current_organization(organization_a)
        data_a = compute_atendimento_hora(organization_a, hour_start=h, foco="todos")
        assert data_a["total"] == 1
        assert data_a["rows"][0]["customer_name"] == "Cliente A"

        set_current_organization(organization_b)
        data_b = compute_atendimento_hora(organization_b, hour_start=h, foco="todos")
        assert data_b["total"] == 1
        assert data_b["rows"][0]["customer_name"] == "Cliente B"

    def test_categorias_juntam_motivos_e_tags_sem_repetir(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(
            organization_a, external_id="a1", opened_at=h,
            motivos=["Quedas"], tags=["Quedas", "LOS"],
        )
        data = compute_atendimento_hora(organization_a, hour_start=h, foco="todos")
        assert data["rows"][0]["categorias"] == ["Quedas", "LOS"]


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoHoraView:
    def test_lista_renderiza(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        _at(
            organization_a, external_id="a1", opened_at=h,
            customer_name="Fulano de Tal", atendente_nome="Ana",
            protocol="2026080312345", tags=["Sem Conexão"],
        )
        client.force_login(user_a)
        resp = client.get(f"{URL}?h={_h_param(h)}&foco=todos")
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "Fulano de Tal" in html
        assert "Ana" in html
        assert "2026080312345" in html
        assert "1 atendimento entre" in html
        assert "Exportar CSV" in html

    def test_h_invalido_redireciona_para_tendencias(
        self, client: Any, user_a: User
    ) -> None:
        client.force_login(user_a)
        resp = client.get(f"{URL}?h=nao-e-data&periodo=7d&foco=rede")
        assert resp.status_code == 302
        assert resp.url.startswith("/operations/atendimento-tendencias/?")
        assert "periodo=7d" in resp.url
        assert "foco=rede" in resp.url

    def test_h_ausente_redireciona(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 302
        assert resp.url.startswith("/operations/atendimento-tendencias/?")

    def test_propaga_periodo_no_link_de_volta(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        h = _hour()
        resp = client.get(f"{URL}?h={_h_param(h)}&de=2026-01-01&ate=2026-01-31")
        assert resp.status_code == 200
        assert "de=2026-01-01&amp;ate=2026-01-31" in resp.content.decode()

    def test_isolamento_cross_tenant_na_view(
        self,
        client: Any,
        user_a: User,
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
        html = client.get(f"{URL}?h={_h_param(h)}&foco=todos").content.decode()
        assert "Cliente Brava" in html
        assert "Cliente ACME" not in html

    def test_sem_acesso_a_aba_de_tendencias(
        self, client: Any, organization_a: Organization
    ) -> None:
        grupo = AccessGroup.objects.create(
            organization=organization_a, name="Só executivo", allowed_pages=["executive"]
        )
        user = User.objects.create_user(email="sem-acesso@acme.test")
        OrganizationMembership.objects.create(
            user=user, organization=organization_a,
            role=OrganizationMembership.Role.MEMBER, is_active=True,
            access_group=grupo,
        )
        client.force_login(user)
        resp = client.get(f"{URL}?h={_h_param(_hour())}")
        assert resp.status_code in (302, 403)
        if resp.status_code == 302:
            assert not resp.url.startswith(URL)

    def test_acesso_herdado_da_aba_de_tendencias(
        self, client: Any, organization_a: Organization
    ) -> None:
        grupo = AccessGroup.objects.create(
            organization=organization_a, name="Atendimento",
            allowed_pages=["atendimento_tendencias"],
        )
        user = User.objects.create_user(email="com-acesso@acme.test")
        OrganizationMembership.objects.create(
            user=user, organization=organization_a,
            role=OrganizationMembership.Role.MEMBER, is_active=True,
            access_group=grupo,
        )
        client.force_login(user)
        assert client.get(f"{URL}?h={_h_param(_hour())}").status_code == 200


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAtendimentoHoraCsv:
    def test_csv_header_linhas_e_bom(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        h = _hour()
        sup = _departamento(organization_a, external_id="dsup", nome="Suporte")
        _at(
            organization_a, external_id="a1", opened_at=h, departamento=sup,
            customer_name="Cliente Ação", customer_document="12345678901",
            atendente_nome="José", protocol="P1", motivos=["Lentidão"],
            tags=["Quedas"],
        )
        _at(
            organization_a, external_id="a2", opened_at=h + timedelta(minutes=30),
            departamento=sup, customer_name="Outro Cliente", protocol="P2",
        )
        client.force_login(user_a)
        resp = client.get(f"{URL}?h={_h_param(h)}&foco=suporte&format=csv")

        assert resp.status_code == 200
        assert resp["Content-Type"].startswith("text/csv")
        filename = f"atendimentos_{h.strftime('%Y-%m-%d_%Hh')}.csv"
        assert resp["Content-Disposition"] == f'attachment; filename="{filename}"'

        raw = resp.content
        assert raw.startswith(b"\xef\xbb\xbf")  # BOM UTF-8
        text = raw.decode("utf-8-sig")
        linhas = [ln for ln in text.split("\r\n") if ln]
        assert linhas[0] == (
            "Cliente;Documento;Horário;Atendente;Departamento;"
            "Categorias;Protocolo;Status"
        )
        assert len(linhas) == 3  # header + 2 atendimentos
        assert linhas[1].startswith("Cliente Ação;12345678901;")
        assert "Lentidão, Quedas" in linhas[1]
        assert ";Suporte;" in linhas[1]
        assert linhas[1].endswith(";P1;Finalizado")

    def test_csv_respeita_o_foco(
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
            f"{URL}?h={_h_param(h)}&foco=comercial&format=csv"
        ).content.decode("utf-8-sig")
        assert "Do Comercial" in text
        assert "Do Suporte" not in text
