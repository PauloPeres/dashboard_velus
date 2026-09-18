"""Testes da aba Quedas & Massivas (#146).

O que estes testes travam não é o layout — é a **honestidade** da tela, que é o
núcleo da feature. Quatro regras, todas vindas de medição em produção
(`docs/massivas-plano.md`):

1. o escopo nunca aparece sozinho: onde há `element_label` há a fração ao lado,
   e quando existe trecho suspeito é ele o destaque. Motivo: o maior evento real
   fecha em escopo OLT com fração 0,02 — dizer "OLT 1 caiu" manda o técnico ao
   lugar errado (§2.6);
2. a tela nunca promete traçado de cabo — a geometria não existe na API (§2.3);
3. o painel de motivos declara a cobertura, porque `motivo_desconexao` vem vazio
   em ~70% das quedas (§2.4);
4. a tela declara de quando é a foto (`ConnectionPollState`) e avisa quando o
   último poll falhou — silêncio seria lido como "ninguém caiu".

Mais o básico: acesso/RBAC, isolamento cross-tenant, o estado vazio (que é o
estado NORMAL desta tela) e a massiva aberta com retorno parcial.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.utils import timezone

from apps.customers.infrastructure.models import Contract, Customer
from apps.dashboards.massivas import (
    _signal_cell,
    _signal_fields_available,
    compute_causas_onu,
    compute_motivos,
    element_references,
    outage_row,
)
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    ConnectionPollState,
    NetworkElement,
    OutageAffectedLogin,
    OutageEvent,
)
from apps.shared.context import set_current_organization
from apps.tenancy.models import AccessGroup, Organization, OrganizationMembership, User

URL = "/operations/massivas/"
URL_PARTIAL = "/operations/massivas/abertas/"


# =============================================================================
# Helpers de seed
# =============================================================================
def _connection(
    org: Organization, *, login: str, contract: str = "", causa_onu: str = ""
) -> Connection:
    set_current_organization(org)
    conn = Connection.objects.create(
        organization=org,
        source_type="IXC",
        external_id=f"conn-{org.slug}-{login}",
        customer_external_id=f"cust-{login}",
        contract_external_id=contract,
        login=login,
        status=Connection.Status.OFFLINE,
    )
    # A causa da OLT (#148) mora na Connection, não na queda — e pode não existir
    # no working tree, então nada de assumir o campo.
    if causa_onu and hasattr(conn, "onu_last_drop_cause"):
        Connection.objects.filter(pk=conn.pk).update(onu_last_drop_cause=causa_onu)
        conn.refresh_from_db()
    return conn


def _drop(
    org: Organization,
    *,
    login: str,
    minutos_atras: int = 20,
    restored: bool = False,
    reason: str = "",
    cto: str = "",
    lat: float | None = None,
    lon: float | None = None,
    contract: str = "",
    customer: Customer | None = None,
    causa_onu: str = "",
) -> ConnectionDropEvent:
    set_current_organization(org)
    now = timezone.now()
    conn = _connection(org, login=login, contract=contract, causa_onu=causa_onu)
    return ConnectionDropEvent.objects.create(
        organization=org,
        connection=conn,
        customer=customer,
        login=login,
        dropped_at=now - timedelta(minutes=minutos_atras),
        restored_at=now - timedelta(minutes=5) if restored else None,
        reason=reason,
        cto_external_id=cto,
        cto_port="3",
        latitude=lat,
        longitude=lon,
        monthly_amount=Decimal("99.90"),
    )


def _outage(
    org: Organization,
    *,
    scope: str = OutageEvent.Scope.OLT,
    element_label: str = "OLT 1",
    element_id: str = "1",
    fraction: float = 0.02,
    segment: str = "",
    confidence: str = OutageEvent.Confidence.MEDIUM,
    affected: int = 5,
    restored: int = 0,
    mrr: str = "499.50",
    ended: bool = False,
) -> OutageEvent:
    set_current_organization(org)
    now = timezone.now()
    return OutageEvent.objects.create(
        organization=org,
        started_at=now - timedelta(minutes=30),
        ended_at=now - timedelta(minutes=2) if ended else None,
        last_detected_at=now,
        scope=scope,
        element_external_id=element_id,
        element_label=element_label,
        suspected_segment_label=segment,
        confidence=confidence,
        affected_count=affected,
        restored_count=restored,
        affected_fraction=fraction,
        mrr_at_risk=Decimal(mrr),
    )


def _poll_state(
    org: Organization, *, sucesso_min: int | None = 2, tentativa_min: int | None = 2
) -> ConnectionPollState:
    set_current_organization(org)
    now = timezone.now()
    return ConnectionPollState.objects.create(
        organization=org,
        baseline_at=now - timedelta(days=1),
        last_poll_at=now - timedelta(minutes=tentativa_min)
        if tentativa_min is not None
        else None,
        last_success_at=now - timedelta(minutes=sucesso_min)
        if sucesso_min is not None
        else None,
    )


def _member(
    org: Organization, *, email: str, pages: list[str] | None
) -> User:
    grupo = (
        AccessGroup.objects.create(organization=org, name="G", allowed_pages=pages)
        if pages is not None
        else None
    )
    user = User.objects.create_user(email=email)
    OrganizationMembership.objects.create(
        user=user,
        organization=org,
        role=OrganizationMembership.Role.MEMBER,
        is_active=True,
        access_group=grupo,
    )
    return user


# =============================================================================
# Acesso e RBAC
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAcesso:
    def test_requires_login(self, client: Any) -> None:
        assert client.get(URL).status_code == 302

    def test_partial_requires_login(self, client: Any) -> None:
        assert client.get(URL_PARTIAL).status_code == 302

    def test_owner_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        assert b"Quedas &amp; Massivas" in resp.content

    def test_membro_com_a_chave_entra(
        self, client: Any, organization_a: Organization
    ) -> None:
        user = _member(organization_a, email="m@a.test", pages=["massivas"])
        client.force_login(user)
        assert client.get(URL).status_code == 200
        assert client.get(URL_PARTIAL).status_code == 200

    def test_membro_sem_a_chave_e_barrado(
        self, client: Any, organization_a: Organization
    ) -> None:
        user = _member(organization_a, email="m2@a.test", pages=["executive"])
        client.force_login(user)
        resp = client.get(URL)
        assert resp.status_code == 302
        assert resp.url == "/executive/"

    def test_detalhe_herda_a_chave_da_aba(
        self, client: Any, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a)
        user = _member(organization_a, email="m3@a.test", pages=["massivas"])
        client.force_login(user)
        assert client.get(f"{URL}{outage.pk}/").status_code == 200

    def test_aba_no_menu(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Quedas &amp; Massivas" in html
        assert URL in html


# =============================================================================
# Isolamento entre tenants
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCrossTenant:
    def test_massiva_de_outra_org_nao_aparece(
        self,
        client: Any,
        user_a: User,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        _outage(organization_b, element_label="OLT DA BRAVA")
        _drop(organization_b, login="brava-1")
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        assert b"OLT DA BRAVA" not in resp.content
        assert resp.context["massivas_abertas"] == 0
        assert resp.context["clientes_fora"] == 0

    def test_detalhe_de_outra_org_da_404(
        self,
        client: Any,
        user_a: User,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        alheia = _outage(organization_b)
        client.force_login(user_a)
        assert client.get(f"{URL}{alheia.pk}/").status_code == 404


# =============================================================================
# Estado vazio — o estado NORMAL desta tela
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestEstadoVazio:
    def test_sem_massiva_a_pagina_renderiza_e_explica(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _poll_state(organization_a)
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "Nenhuma massiva aberta agora" in html
        assert "estado normal" in html
        assert resp.context["clientes_fora"] == 0
        assert resp.context["maior_massiva"] is None

    def test_queda_isolada_nao_vira_massiva_mas_aparece_no_kpi(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _drop(organization_a, login="solo")
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["clientes_fora"] == 1
        assert resp.context["massivas_abertas"] == 0
        assert b"sem formar" in resp.content


# =============================================================================
# Massiva aberta com retorno parcial
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestMassivaAbertaComRetornoParcial:
    def test_kpis_e_progresso_de_retorno(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _poll_state(organization_a)
        outage = _outage(
            organization_a,
            scope=OutageEvent.Scope.CTO,
            element_label="B41-SP03",
            fraction=1.0,
            confidence=OutageEvent.Confidence.HIGH,
            affected=5,
            restored=2,
        )
        for i in range(3):
            _drop(organization_a, login=f"fora-{i}", cto="41")

        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.status_code == 200
        linha = resp.context["linhas"][0]
        assert linha["id"] == outage.pk
        assert linha["ainda_fora"] == 3
        assert linha["restored_pct"] == 40
        assert resp.context["clientes_fora"] == 3
        assert resp.context["massivas_abertas"] == 1
        html = resp.content.decode()
        assert "40% já voltou" in html
        assert "B41-SP03" in html

    def test_detalhe_lista_clientes_com_quem_voltou(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        cliente = Customer.objects.create(
            organization=organization_a,
            source_type="IXC",
            external_id="cust-1",
            document="12345678901",
            name="Maria Cliente",
            phone="47999990000",
        )
        Contract.objects.create(
            organization=organization_a,
            source_type="IXC",
            external_id="ctr-1",
            customer=cliente,
            customer_external_id="cust-1",
            plan_name="Fibra 500MB",
            monthly_amount=Decimal("99.90"),
            status=Contract.Status.ACTIVE,
        )
        outage = _outage(organization_a, affected=2, restored=1)
        fora = _drop(organization_a, login="ainda-fora", contract="ctr-1", customer=cliente)
        voltou = _drop(organization_a, login="voltou", restored=True, contract="ctr-1")
        for drop in (fora, voltou):
            OutageAffectedLogin.objects.create(
                organization=organization_a,
                outage=outage,
                drop_event=drop,
                login=drop.login,
                dropped_at=drop.dropped_at,
                restored_at=drop.restored_at,
                monthly_amount=drop.monthly_amount,
            )

        client.force_login(user_a)
        html = client.get(f"{URL}{outage.pk}/").content.decode()
        assert "Maria Cliente" in html
        assert "Fibra 500MB" in html
        assert "47999990000" in html
        assert "ainda fora" in html


# =============================================================================
# Regra 1 — o escopo nunca aparece sozinho
# =============================================================================
@pytest.mark.django_db
class TestRegra1EscopoNuncaSozinho:
    def test_rotulo_do_elemento_carrega_a_fracao(
        self, organization_a: Organization
    ) -> None:
        outage = _outage(
            organization_a, scope=OutageEvent.Scope.OLT, element_label="OLT 1",
            fraction=0.02,
        )
        linha = outage_row(outage)
        assert linha["elemento_frase"] == "OLT 1 — 2% dos logins da OLT"

    def test_fracao_minuscula_nao_vira_zero_por_cento(
        self, organization_a: Organization
    ) -> None:
        linha = outage_row(_outage(organization_a, fraction=0.004))
        assert "<1%" in linha["elemento_frase"]

    def test_escopo_alto_com_fracao_baixa_ganha_ressalva(
        self, organization_a: Organization
    ) -> None:
        linha = outage_row(_outage(organization_a, fraction=0.02))
        assert "continuam no ar" in linha["ressalva_escopo"]
        assert "não a OLT inteira" in linha["ressalva_escopo"]

    def test_cto_cheia_nao_ganha_ressalva(self, organization_a: Organization) -> None:
        linha = outage_row(
            _outage(
                organization_a, scope=OutageEvent.Scope.CTO,
                element_label="B41-SP03", fraction=1.0,
            )
        )
        assert linha["ressalva_escopo"] == ""

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_tela_nunca_mostra_o_elemento_sem_a_fracao(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(organization_a, element_label="OLT 1", fraction=0.02)
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        # Toda ocorrência do rótulo vem colada na fração — nunca "OLT 1" solto.
        assert "OLT 1 — 2% dos logins da OLT" in html
        assert html.count("OLT 1") == html.count("OLT 1 — 2% dos logins da OLT")
        assert "continuam no ar" in html

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_trecho_suspeito_ganha_o_destaque_visual(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(
            organization_a,
            element_label="OLT 1",
            fraction=0.02,
            segment="trecho B47 - SP11 → B47 - SP12",
        )
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        destaque = html.index("trecho B47 - SP11")
        elemento = html.index("OLT 1 — 2% dos logins da OLT")
        # O trecho vem antes e no bloco de destaque; o elemento fica de contexto.
        assert destaque < elemento
        assert "Trecho suspeito" in html
        assert 'text-lg font-bold text-gray-900 leading-snug">trecho B47' in html


# =============================================================================
# Regra 2 — nada de traçado de cabo
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestRegra2SemTracadoDeCabo:
    def test_mapa_declara_que_nao_desenha_cabo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Nenhum cabo é desenhado" in html
        assert "não expõe a geometria dos cabos" in html

    def test_segmento_e_rotulado_como_trecho_suspeito(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, segment="trecho B43-SP02 → B43-SP03")
        client.force_login(user_a)
        for url in (URL, f"{URL}{outage.pk}/"):
            html = client.get(url).content.decode()
            assert "Trecho suspeito" in html
            # Nunca "cabo X rompido": a única menção a cabo é a do limite declarado.
            assert "cabo B43" not in html
            assert "rompido" not in html.replace(
                '"cabo X rompido"', ""
            ).replace("cabo X rompido", "")


# =============================================================================
# Regra 3 — painel de motivos declara a cobertura
# =============================================================================
@pytest.mark.django_db
class TestRegra3CoberturaDeMotivos:
    def test_cobertura_calculada_sobre_o_total(self) -> None:
        quedas = [
            SimpleNamespace(reason="NAS-Request"),
            SimpleNamespace(reason=""),
            SimpleNamespace(reason=""),
        ]
        motivos = compute_motivos(quedas)  # type: ignore[arg-type]
        assert motivos["total"] == 3
        assert motivos["conhecidos"] == 1
        assert motivos["sem_motivo"] == 2
        assert motivos["cobertura_pct"] == 33
        # O percentual da fatia é sobre os CONHECIDOS, e a tela diz isso.
        assert motivos["linhas"][0]["pct_dos_conhecidos"] == 100

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_tela_declara_n_de_m(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _drop(organization_a, login="com-motivo", reason="NAS-Request")
        _drop(organization_a, login="sem-motivo-1")
        _drop(organization_a, login="sem-motivo-2")
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert 'Motivo conhecido em <span class="font-semibold">1</span>' in html
        assert '<span class="font-semibold">3</span> quedas' in html
        assert "33% de cobertura" in html
        assert "Sem motivo informado" in html
        # Desde a #148 o RADIUS é o painel secundário, e a tela diz isso.
        assert "Motivo do RADIUS" in html
        assert "(secundário)" in html


# =============================================================================
# Causa da ONU — o discriminador que o RADIUS nunca deu (#148)
# =============================================================================
@pytest.mark.django_db
class TestCausaDaOnu:
    def test_vazio_e_hifen_sao_ausencia(self) -> None:
        quedas = [
            SimpleNamespace(connection=SimpleNamespace(onu_last_drop_cause="dying-gasp")),
            SimpleNamespace(connection=SimpleNamespace(onu_last_drop_cause="-")),
            SimpleNamespace(connection=SimpleNamespace(onu_last_drop_cause="")),
            SimpleNamespace(connection=None),
        ]
        causas = compute_causas_onu(quedas)  # type: ignore[arg-type]
        assert causas["total"] == 4
        assert causas["conhecidos"] == 1
        assert causas["sem_causa"] == 3
        assert causas["cobertura_pct"] == 25
        assert [linha["causa"] for linha in causas["linhas"]] == ["dying-gasp"]

    def test_rotulos_saem_crus_sem_traducao(self) -> None:
        quedas = [
            SimpleNamespace(connection=SimpleNamespace(onu_last_drop_cause="LOSi/LOBi")),
            SimpleNamespace(connection=SimpleNamespace(onu_last_drop_cause="LOSi/LOBi")),
            SimpleNamespace(connection=SimpleNamespace(onu_last_drop_cause="dying-gasp")),
        ]
        causas = compute_causas_onu(quedas)  # type: ignore[arg-type]
        assert causas["linhas"][0] == {
            "causa": "LOSi/LOBi", "quedas": 2, "pct_dos_conhecidos": 67,
        }

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_painel_principal_declara_cobertura_e_nao_conclui(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        if not hasattr(Connection, "onu_last_drop_cause"):
            pytest.skip("campo de causa da ONU ainda não existe (#148)")

        _drop(organization_a, login="energia", causa_onu="dying-gasp")
        _drop(organization_a, login="fibra", causa_onu="LOS")
        _drop(organization_a, login="mudo")
        _drop(organization_a, login="sentinela", causa_onu="-")

        client.force_login(user_a)
        resp = client.get(URL)
        causas = resp.context["causas_onu"]
        assert causas["conhecidos"] == 2
        assert causas["sem_causa"] == 2
        html = resp.content.decode()
        assert "Causa informada pela ONU" in html
        assert 'Causa conhecida em <span class="font-semibold">2</span>' in html
        assert "50% de cobertura" in html
        assert "dying-gasp" in html
        assert "A OLT não informou" in html
        # A legenda explica o termo; a tela não afirma o diagnóstico.
        assert "não conclui nada" in html
        assert "foi falta de energia" not in html

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_detalhe_traz_a_distribuicao_daquela_massiva(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        if not hasattr(Connection, "onu_last_drop_cause"):
            pytest.skip("campo de causa da ONU ainda não existe (#148)")

        outage = _outage(organization_a, affected=2)
        dentro = _drop(organization_a, login="dentro", causa_onu="dying-gasp")
        OutageAffectedLogin.objects.create(
            organization=organization_a,
            outage=outage,
            drop_event=dentro,
            login=dentro.login,
            dropped_at=dentro.dropped_at,
        )
        # Queda de fora da massiva não pode entrar na distribuição dela.
        _drop(organization_a, login="fora-da-massiva", causa_onu="LOS")

        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        causas = resp.context["causas_onu"]
        assert causas["total"] == 1
        assert [linha["causa"] for linha in causas["linhas"]] == ["dying-gasp"]


# =============================================================================
# Regra 4 — de quando é a foto
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestRegra4IdadeDaFoto:
    def test_sem_poll_a_tela_avisa_que_nao_sabe(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["poll"]["observando"] is False
        html = resp.content.decode()
        assert "ainda não rodou" in html
        assert "não quer dizer que ninguém caiu" in html

    def test_poll_recente_mostra_a_hora_da_foto(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _poll_state(organization_a, sucesso_min=2, tentativa_min=2)
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["poll"]["ok"] is True
        assert "Foto de" in resp.content.decode()

    def test_ultima_tentativa_falhou_e_declarado(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        # Tentativa mais nova que o último sucesso = a rodada falhou.
        _poll_state(organization_a, sucesso_min=40, tentativa_min=1)
        client.force_login(user_a)
        resp = client.get(URL)
        poll = resp.context["poll"]
        assert poll["falhou"] is True
        assert poll["ok"] is False
        html = resp.content.decode()
        assert "última tentativa de leitura falhou" in html
        assert "ainda não apareceram aqui" in html

    def test_foto_velha_e_declarada(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _poll_state(organization_a, sucesso_min=45, tentativa_min=45)
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["poll"]["velha"] is True
        assert "a foto está velha" in resp.content.decode()

    def test_lista_vazia_com_poll_ruim_avisa_a_ambiguidade(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _poll_state(organization_a, sucesso_min=45, tentativa_min=45)
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "pode ser falta de leitura, não ausência de queda" in html


# =============================================================================
# Auto-refresh e mapa
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAutoRefreshEMapa:
    def test_bloco_de_tempo_real_se_recarrega_sozinho(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert f'hx-get="{URL_PARTIAL}"' in html
        # Alinhado ao poll de 3 min: a 60s, 2 de 3 chamadas trariam a mesma foto.
        assert 'hx-trigger="every 180s"' in html
        assert 'hx-swap="outerHTML"' in html

    def test_partial_devolve_so_o_bloco(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(organization_a, element_label="OLT 1", fraction=0.02)
        client.force_login(user_a)
        html = client.get(URL_PARTIAL).content.decode()
        assert "OLT 1 — 2% dos logins da OLT" in html
        assert "<html" not in html
        assert "massivas-mapa" not in html

    def test_mapa_usa_basemap_proprio_e_marca_quem_nao_tem_coordenada(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        NetworkElement.objects.create(
            organization=organization_a,
            source_type="IXC",
            kind=NetworkElement.Kind.CTO,
            external_id="41",
            name="B41-SP03",
            latitude=-26.9,
            longitude=-48.6,
        )
        # As duas quedas precisam pertencer a uma massiva aberta: o mapa da tela
        # geral só desenha o que está vinculado a uma.
        outage = _outage(organization_a, scope=OutageEvent.Scope.CTO, element_id="41")
        for login, lat, lon in (("com-geo", -26.9, -48.6), ("sem-geo", None, None)):
            drop = _drop(organization_a, login=login, cto="41", lat=lat, lon=lon)
            OutageAffectedLogin.objects.create(
                organization=organization_a,
                outage=outage,
                drop_event=drop,
                login=drop.login,
                dropped_at=drop.dropped_at,
            )

        client.force_login(user_a)
        resp = client.get(URL)
        html = resp.content.decode()
        # O tile server do OSM bloqueia uso por aplicação: o basemap tem que
        # sair de um provedor que permita, com a atribuição junto.
        assert "tiles.openfreemap.org" in html
        assert "tile.openstreetmap.org" not in html
        assert resp.context["mapa"]["sem_coordenada"] == 1
        assert "não têm coordenada" in html


# =============================================================================
# Ver o cliente voltando — a outra metade da razão de ser da tela
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestRetornoVisivel:
    def test_quem_voltou_vira_camada_verde_no_mapa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, affected=2, restored=1)
        for login, voltou, lat, lon in (
            ("fora", False, -26.9, -48.6),
            ("voltou", True, -26.8, -48.5),
        ):
            drop = _drop(
                organization_a, login=login, restored=voltou, lat=lat, lon=lon
            )
            OutageAffectedLogin.objects.create(
                organization=organization_a,
                outage=outage,
                drop_event=drop,
                login=drop.login,
                dropped_at=drop.dropped_at,
                restored_at=drop.restored_at,
            )

        client.force_login(user_a)
        resp = client.get(URL)
        mapa = resp.context["mapa"]
        assert len(mapa["clientes"]) == 1
        assert len(mapa["voltaram"]) == 1
        # O rótulo carrega a hora — "verde" sem quando não ajuda no reparo.
        assert "voltou" in mapa["voltaram"][0]["label"]
        assert "caiu" in mapa["clientes"][0]["label"]
        # A legenda do traço vive no JSON do Plotly (com escape unicode), então
        # o que se verifica no HTML é a legenda escrita da própria seção.
        assert "Verde: já voltou" in resp.content.decode()

    def test_mapa_da_tela_geral_ignora_queda_fora_de_massiva(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Queda individual não polui o mapa — mas a tela diz que ela existe.

        Numa base grande há sempre quedas soltas (ONU na tomada, mudança de
        endereço). Elas enchiam o mapa de vermelho sem relação com o rompimento
        em atendimento. Somem do mapa, seguem contadas em "clientes fora", e a
        nota do mapa declara quantas ficaram de fora.
        """
        outage = _outage(organization_a, affected=1)
        na_massiva = _drop(organization_a, login="na-massiva", lat=-26.9, lon=-48.6)
        OutageAffectedLogin.objects.create(
            organization=organization_a,
            outage=outage,
            drop_event=na_massiva,
            login=na_massiva.login,
            dropped_at=na_massiva.dropped_at,
        )
        _drop(organization_a, login="avulso", lat=-26.5, lon=-48.1)

        client.force_login(user_a)
        resp = client.get(URL)
        mapa = resp.context["mapa"]
        assert [p["label"].split(" ·")[0] for p in mapa["clientes"]] == ["na-massiva"]
        # O avulso não sumiu da tela: só saiu do mapa.
        assert resp.context["clientes_fora"] == 2
        assert "1 queda(s) individual(is)" in resp.content.decode()

    def test_detalhe_mantem_quem_voltou_no_mapa_em_verde(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, affected=2, restored=1, ended=True)
        for login, voltou in (("fora", False), ("voltou", True)):
            drop = _drop(
                organization_a, login=login, restored=voltou, lat=-26.9, lon=-48.6
            )
            OutageAffectedLogin.objects.create(
                organization=organization_a,
                outage=outage,
                drop_event=drop,
                login=drop.login,
                dropped_at=drop.dropped_at,
                restored_at=drop.restored_at,
            )
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        mapa = resp.context["mapa"]
        # O post-mortem não esvazia o mapa: quem voltou continua lá, em verde.
        assert len(mapa["voltaram"]) == 1
        assert len(mapa["clientes"]) == 1


# =============================================================================
# Carimbo do que NÃO acompanha o auto-refresh
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCarimboDosBlocosEstaticos:
    def test_mapa_e_timeline_declaram_quando_foram_desenhados(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        # Um por bloco (mapa e linha do tempo).
        assert html.count("não acompanha o auto-refresh") == 2
        assert "desenhado às" in html

    def test_detalhe_tambem_carimba_o_mapa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a)
        client.force_login(user_a)
        html = client.get(f"{URL}{outage.pk}/").content.decode()
        assert "desenhado às" in html


# =============================================================================
# Rótulo do dinheiro — não pode ser lido como MRR perdido
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestRotuloDaMensalidade:
    def test_kpi_nao_se_chama_mrr(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(organization_a, mrr="1234.00")
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Mensalidade dos afetados" in html
        assert "MRR afetado" not in html
        assert "R$ 1.234" in html


# =============================================================================
# Referência humana do elemento em escopo
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestReferenciaHumana:
    def _planta(self, org: Organization) -> None:
        set_current_organization(org)
        NetworkElement.objects.create(
            organization=org,
            source_type="IXC",
            kind=NetworkElement.Kind.POP,
            external_id="7",
            name="Portal do Pirapora",
            latitude=-26.9,
            longitude=-48.6,
        )
        NetworkElement.objects.create(
            organization=org,
            source_type="IXC",
            kind=NetworkElement.Kind.OLT,
            external_id="1",
            name="OLT 1",
            parent_kind=NetworkElement.Kind.POP,
            parent_external_id="7",
        )

    def test_olt_ganha_o_pop_ao_lado(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        self._planta(organization_a)
        _outage(organization_a, element_label="OLT 1", element_id="1", fraction=0.02)
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["linhas"][0]["referencia"] == "no POP Portal do Pirapora"
        html = resp.content.decode()
        # A fração continua colada no elemento; a referência é acréscimo.
        assert "OLT 1 — 2% dos logins da OLT" in html
        assert "no POP Portal do Pirapora" in html

    def test_sem_cadastro_nao_inventa_lugar(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(organization_a, element_label="OLT 9", element_id="9")
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["linhas"][0]["referencia"] == ""

    def test_nome_igual_ao_id_nao_e_referencia(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        NetworkElement.objects.create(
            organization=organization_a,
            source_type="IXC",
            kind=NetworkElement.Kind.POP,
            external_id="3",
            name="3",
        )
        outage = _outage(
            organization_a, scope=OutageEvent.Scope.POP, element_label="POP 3",
            element_id="3", fraction=0.1,
        )
        assert element_references(organization_a, [outage]) == {}

    def test_cto_nao_recebe_referencia(self, organization_a: Organization) -> None:
        outage = _outage(
            organization_a, scope=OutageEvent.Scope.CTO, element_label="B41-SP03",
            element_id="41", fraction=1.0,
        )
        assert element_references(organization_a, [outage]) == {}


# =============================================================================
# Sinal óptico (#148) — renderizado defensivamente enquanto os campos não vêm
# =============================================================================
@pytest.mark.django_db
class TestSinalOptico:
    def test_ausencia_de_campo_vira_sem_leitura(self) -> None:
        celula = _signal_cell(SimpleNamespace())  # type: ignore[arg-type]
        assert celula["antes_str"] == "sem leitura"
        assert celula["depois_str"] == "sem leitura"
        assert celula["degradado"] is False

    def test_zero_dbm_e_ausencia_de_leitura_nao_zero(self) -> None:
        celula = _signal_cell(
            SimpleNamespace(signal_rx_before=0.0, signal_rx_after=0.0)  # type: ignore[arg-type]
        )
        assert celula["antes_str"] == "sem leitura"
        assert "0,00 dBm" not in celula["depois_str"]

    def test_leitura_valida_traz_valor_e_carimbo(self) -> None:
        agora = timezone.now()
        celula = _signal_cell(
            SimpleNamespace(  # type: ignore[arg-type]
                signal_rx_before=-24.43,
                signal_before_measured_at=agora - timedelta(hours=18),
                signal_rx_after=-24.10,
                signal_after_measured_at=agora,
            )
        )
        assert celula["antes_str"] == "-24,43 dBm"
        assert celula["antes_em"] is not None
        assert celula["depois_em"] is not None
        assert celula["degradado"] is False

    def test_retorno_pior_que_a_base_e_sinalizado(self) -> None:
        celula = _signal_cell(
            SimpleNamespace(signal_rx_before=-24.0, signal_rx_after=-28.5)  # type: ignore[arg-type]
        )
        assert celula["degradado"] is True
        assert celula["delta_str"].startswith("-4,5")

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_tabela_do_detalhe_nao_quebra_sem_os_campos(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, affected=1)
        drop = _drop(organization_a, login="sem-sinal")
        OutageAffectedLogin.objects.create(
            organization=organization_a,
            outage=outage,
            drop_event=drop,
            login=drop.login,
            dropped_at=drop.dropped_at,
        )
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "sem leitura" in html
        assert "0,00 dBm" not in html


    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_detalhe_marca_retorno_pior_que_a_base(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Quando as colunas da #148 existem, a piora de 3 dB é sinalizada."""
        if not _signal_fields_available():
            pytest.skip("colunas de sinal óptico ainda não existem (#148)")

        outage = _outage(organization_a, affected=1)
        drop = _drop(organization_a, login="fusao-ruim")
        agora = timezone.now()
        ConnectionDropEvent.objects.filter(pk=drop.pk).update(
            signal_rx_before=-24.43,
            signal_before_measured_at=agora - timedelta(hours=18),
            signal_rx_after=-29.10,
            signal_after_measured_at=agora,
        )
        OutageAffectedLogin.objects.create(
            organization=organization_a,
            outage=outage,
            drop_event=drop,
            login=drop.login,
            dropped_at=drop.dropped_at,
        )
        client.force_login(user_a)
        html = client.get(f"{URL}{outage.pk}/").content.decode()
        assert "-24,43 dBm" in html
        assert "-29,10 dBm" in html
        assert "revisar fusão" in html
        # O carimbo de cada leitura aparece — a base pode ter 18h.
        assert html.count("medido ") >= 2


# =============================================================================
# Histórico — o agregado, sem analytics do passado
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestHistorico:
    def test_massiva_encerrada_sai_das_abertas_e_entra_no_historico(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(
            organization_a, element_label="OLT 2", element_id="2",
            fraction=0.03, affected=8, restored=8, ended=True,
        )
        client.force_login(user_a)
        resp = client.get(URL)
        assert resp.context["massivas_abertas"] == 0
        assert len(resp.context["historico"]) == 1
        historico = resp.context["historico"][0]
        assert historico["duracao_min"] == 28
        # Mesmo no histórico o elemento não aparece sem a fração.
        assert historico["elemento_frase"] == "OLT 2 — 3% dos logins da OLT"
