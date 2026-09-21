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
    compute_cabos_candidatos,
    compute_causas_onu,
    compute_motivos,
    compute_reincidencia,
    compute_veredito,
    compute_vizinhanca,
    compute_mapa,
    element_references,
    outage_row,
)
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    ConnectionPollState,
    NetworkElement,
    NetworkElementGeometry,
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
    inicio_min_atras: int = 30,
) -> OutageEvent:
    set_current_organization(org)
    now = timezone.now()
    return OutageEvent.objects.create(
        organization=org,
        started_at=now - timedelta(minutes=inicio_min_atras),
        ended_at=now - timedelta(minutes=inicio_min_atras - 28) if ended else None,
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
    def test_mapa_separa_inferencia_de_tracado_real(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Desde o spike R2 existe linha cheia no mapa — o traçado do cabo.

        A regra que sobrevive não é "não desenhe cabo", é "não diga que ele
        rompeu": o estilo da linha tem que separar o que é inferência de
        cadastro do que é geometria de verdade, e a legenda tem que dizer que
        nem a geometria prova rompimento.
        """
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "é o traçado do cabo desenhado no projeto do InMap" in html
        assert "cadastro, não leitura" in html
        assert 'nunca "cabo X rompido"' in html

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
        # o que se verifica no HTML é a legenda escrita da própria seção — que
        # desde 21/09/2026 mora dentro do "Como ler o mapa", recolhida por
        # padrão. Recolhida, não removida: o HTML continua trazendo o texto.
        assert "verde já voltou" in resp.content.decode()

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


# =============================================================================
# Veredito de causa provável (R7) — a tela diz se manda viatura ou não
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestVeredito:
    def test_dying_gasp_em_massa_aponta_energia(
        self, organization_a: Organization
    ) -> None:
        """Numa massiva de dying-gasp não se manda equipe — espera-se a luz voltar."""
        quedas = [
            _drop(organization_a, login=f"e{i}", causa_onu="dying-gasp")
            for i in range(8)
        ]
        v = compute_veredito(quedas, scope="GEO")
        assert v["veredito"] == "energia"
        assert "dying-gasp" in v["base"]
        # GEO + energia se reforçam: a queda seguiu o alimentador, não a rede.
        assert "geográfico" in v["reforco"]

    def test_los_em_massa_aponta_fibra(self, organization_a: Organization) -> None:
        quedas = [
            _drop(organization_a, login=f"f{i}", causa_onu="LOSi/LOBi")
            for i in range(8)
        ]
        v = compute_veredito(quedas, scope="PON")
        assert v["veredito"] == "fibra"
        assert "topologia" in v["reforco"]

    def test_energia_com_escopo_topologico_ganha_ressalva(
        self, organization_a: Organization
    ) -> None:
        """Dying-gasp numa PON é estranho: se fosse a concessionária, pegaria
        clientes de outras PONs junto. A tela aponta a contradição em vez de
        engolir o veredito."""
        quedas = [
            _drop(organization_a, login=f"g{i}", causa_onu="dying-gasp")
            for i in range(8)
        ]
        v = compute_veredito(quedas, scope="PON")
        assert v["veredito"] == "energia"
        assert "topologia" in v["reforco"]

    def test_metade_e_metade_nao_vira_veredito(
        self, organization_a: Organization
    ) -> None:
        quedas = [
            _drop(organization_a, login=f"m{i}", causa_onu="dying-gasp")
            for i in range(5)
        ] + [
            _drop(organization_a, login=f"n{i}", causa_onu="LOS") for i in range(5)
        ]
        v = compute_veredito(quedas, scope="OLT")
        assert v["veredito"] == "misto"
        assert v["pct_energia"] == 50
        assert v["pct_fibra"] == 50

    def test_poucas_quedas_nao_geram_veredito(
        self, organization_a: Organization
    ) -> None:
        """2 de 3 quedas viram '67% de energia' — número que parece medida e é
        coincidência. Abaixo do mínimo a tela se recusa a opinar."""
        quedas = [
            _drop(organization_a, login=f"p{i}", causa_onu="dying-gasp")
            for i in range(3)
        ]
        v = compute_veredito(quedas, scope="CTO")
        assert v["veredito"] == "sem_base"

    def test_cobertura_baixa_nao_gera_veredito(
        self, organization_a: Organization
    ) -> None:
        """Uma causa conhecida em dez quedas não descreve a massiva."""
        quedas = [_drop(organization_a, login=f"q{i}") for i in range(9)]
        quedas.append(_drop(organization_a, login="q9", causa_onu="dying-gasp"))
        v = compute_veredito(quedas, scope="OLT")
        assert v["veredito"] == "sem_base"
        assert v["cobertura_pct"] == 10

    def test_timestamp_sintetico_fica_fora_da_cronologia(
        self, organization_a: Organization
    ) -> None:
        """Na partida a frio, 189 quedas ficaram com o MESMO horário ao
        microssegundo — era o relógio do poll, não o IXC. Ler isso como "todas
        no mesmo instante" seria inventar simultaneidade a partir de ausência de
        dado. Horário do IXC vem com microssegundo zero."""
        quedas = [
            _drop(organization_a, login=f"s{i}", causa_onu="dying-gasp")
            for i in range(6)
        ]
        agora = timezone.now().replace(microsecond=123456)
        for q in quedas:
            q.dropped_at = agora
        v = compute_veredito(quedas, scope="GEO")
        assert v["cronologia"]["medivel"] is False
        assert v["cronologia"]["com_hora_real"] == 0

    def test_cronologia_mede_espalhamento_com_hora_do_ixc(
        self, organization_a: Organization
    ) -> None:
        quedas = [
            _drop(organization_a, login=f"t{i}", causa_onu="LOS") for i in range(6)
        ]
        base = timezone.now().replace(microsecond=0) - timedelta(minutes=10)
        for i, q in enumerate(quedas):
            q.dropped_at = base + timedelta(seconds=i * 30)
        v = compute_veredito(quedas, scope="PON")
        assert v["cronologia"]["medivel"] is True
        assert v["cronologia"]["spread_segundos"] == 150
        assert v["cronologia"]["spread_str"] == "2 min"

    def test_card_da_massiva_mostra_o_veredito(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, affected=8)
        for i in range(8):
            drop = _drop(organization_a, login=f"v{i}", causa_onu="dying-gasp")
            OutageAffectedLogin.objects.create(
                organization=organization_a,
                outage=outage,
                drop_event=drop,
                login=drop.login,
                dropped_at=drop.dropped_at,
            )
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Causa provável" in html
        assert "provável falta de energia" in html
        # A base do veredito anda junto com ele — nunca o rótulo sozinho.
        assert "Causa conhecida em 8 de 8 quedas" in html


# =============================================================================
# Quem NÃO caiu no mesmo caminho (R6) — o que delimita o trecho
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestVizinhanca:
    def _conn(
        self, org: Organization, *, login: str, cto: str, pon: str, contrato: str = ""
    ) -> Connection:
        set_current_organization(org)
        return Connection.objects.create(
            organization=org,
            source_type="IXC",
            external_id=f"c-{login}",
            customer_external_id=f"cu-{login}",
            contract_external_id=contrato,
            login=login,
            pon_external_id=pon,
            cto_external_id=cto,
            status=Connection.Status.ONLINE,
        )

    def test_caixa_intacta_na_mesma_pon_aparece_primeiro(
        self, organization_a: Organization
    ) -> None:
        """A caixa que escapou é o que marca o limite do trecho, então ela
        encabeça a lista — não some no meio das que também caíram."""
        set_current_organization(organization_a)
        # A massiva: 2 logins da CTO-10, na PON 7.
        self._conn(organization_a, login="a1", cto="CTO-10", pon="7")
        self._conn(organization_a, login="a2", cto="CTO-10", pon="7")
        # A vizinha intacta: 3 logins na mesma PON, outra caixa.
        for i in range(3):
            self._conn(organization_a, login=f"b{i}", cto="CTO-11", pon="7")
        # Uma vizinha que também tem gente fora.
        for i in range(2):
            self._conn(organization_a, login=f"c{i}", cto="CTO-12", pon="7")
        _drop(organization_a, login="c0", cto="CTO-12")

        quedas = [_drop(organization_a, login="a1", cto="CTO-10")]
        quedas[0].connection.pon_external_id = "7"

        z = compute_vizinhanca(organization_a, quedas)
        assert z["determinavel"] is True
        assert z["n_irmas"] == 2
        # Intacta primeiro.
        assert z["irmas"][0]["cto"] == "CTO-11"
        assert z["irmas"][0]["intacta"] is True
        assert z["irmas"][0]["de_pe"] == 3
        assert z["n_intactas"] == 1

    def test_sem_pon_no_cadastro_o_bloco_se_recusa(
        self, organization_a: Organization
    ) -> None:
        """Sem PON não há irmã. Cair para 'mesma OLT' traria centenas de caixas
        sem relação com o trecho — pior que não responder."""
        quedas = [_drop(organization_a, login="x1", cto="CTO-10")]
        z = compute_vizinhanca(organization_a, quedas)
        assert z["determinavel"] is False
        assert "porta PON" in z["motivo"]

    def test_caixa_so_com_contrato_inativo_nao_vira_caixa_intacta(
        self, organization_a: Organization
    ) -> None:
        """Caixa cujo único login é de contrato cancelado tem denominador zero.

        Ela é irmã pela PON, mas dizer "0/0 fora — intacta" seria oferecer como
        limite do trecho uma caixa onde não há ninguém para cair. O denominador
        é o mesmo do detector (contrato ATIVO), então ela sai como sem contagem
        no cadastro e a tela declara isso.
        """
        set_current_organization(organization_a)
        Contract.objects.create(
            organization=organization_a,
            source_type="IXC",
            external_id="ctr-cancelado",
            customer_external_id="cust-9",
            plan_name="Fibra 500MB",
            monthly_amount=Decimal("99.90"),
            status=Contract.Status.CANCELED,
        )
        self._conn(organization_a, login="d1", cto="CTO-20", pon="9")
        self._conn(
            organization_a, login="d2", cto="CTO-21", pon="9",
            contrato="ctr-cancelado",
        )

        quedas = [_drop(organization_a, login="d1", cto="CTO-20")]
        quedas[0].connection.pon_external_id = "9"
        z = compute_vizinhanca(organization_a, quedas)
        irma = next(i for i in z["irmas"] if i["cto"] == "CTO-21")
        assert irma["total"] == 0
        assert irma["intacta"] is False
        assert z["sem_denominador"] == 1
        assert z["n_intactas"] == 0

    def test_card_mostra_a_vizinhanca(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        outage = _outage(organization_a, affected=5)
        NetworkElement.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.CTO, external_id="CTO-31", name="B31-SP01",
        )
        for i in range(3):
            self._conn(organization_a, login=f"viz{i}", cto="CTO-31", pon="5")
        for i in range(5):
            drop = _drop(organization_a, login=f"cai{i}", cto="CTO-30")
            drop.connection.pon_external_id = "5"
            drop.connection.save(update_fields=["pon_external_id"])
            OutageAffectedLogin.objects.create(
                organization=organization_a, outage=outage, drop_event=drop,
                login=drop.login, dropped_at=drop.dropped_at,
            )
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Quem não caiu no mesmo caminho" in html
        assert "B31-SP01" in html
        assert "0/3 fora" in html


# =============================================================================
# Ligações no mapa (R3) — vínculo de cadastro, nunca traçado de cabo
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestLigacoesDoMapa:
    def _planta(self, org: Organization) -> None:
        set_current_organization(org)
        NetworkElement.objects.create(
            organization=org, source_type="IXC", kind=NetworkElement.Kind.POP,
            external_id="POP-1", name="POP Centro",
            latitude=-23.500, longitude=-47.450,
        )
        NetworkElement.objects.create(
            organization=org, source_type="IXC", kind=NetworkElement.Kind.OLT,
            external_id="OLT-1", name="OLT 1",
            parent_kind=NetworkElement.Kind.POP, parent_external_id="POP-1",
        )
        # Perto do POP e longe dele — o sentido do trecho depende disso.
        for ext, nome, lat in (("CTO-P", "perto", -23.502), ("CTO-L", "longe", -23.560)):
            NetworkElement.objects.create(
                organization=org, source_type="IXC", kind=NetworkElement.Kind.CTO,
                external_id=ext, name=nome, latitude=lat, longitude=-47.450,
                parent_kind=NetworkElement.Kind.OLT, parent_external_id="OLT-1",
            )

    def test_liga_cada_caixa_ao_pop_que_a_alimenta(
        self, organization_a: Organization
    ) -> None:
        """A OLT não tem coordenada em nenhuma das três da planta real, então a
        ponta de cima da ligação é o POP — que a OLT aponta."""
        self._planta(organization_a)
        quedas = [
            _drop(organization_a, login="x", cto="CTO-P", lat=-23.502, lon=-47.450)
        ]
        mapa = compute_mapa(organization_a, quedas)
        assert len(mapa["ligacoes"]) == 1
        assert mapa["ligacoes"][0]["para"] == (-23.500, -47.450)
        assert "POP Centro" in mapa["ligacoes"][0]["label"]

    def test_trecho_sai_da_caixa_mais_proxima_do_pop(
        self, organization_a: Organization
    ) -> None:
        """O sentido tem que bater com o rótulo do trecho suspeito: se a tela
        desenhasse a seta para um lado e o texto dissesse outro, uma das duas
        estaria mentindo."""
        self._planta(organization_a)
        quedas = [
            _drop(organization_a, login="a", cto="CTO-P", lat=-23.502, lon=-47.450),
            _drop(organization_a, login="b", cto="CTO-L", lat=-23.560, lon=-47.450),
        ]
        mapa = compute_mapa(organization_a, quedas)
        assert len(mapa["trecho"]) == 1
        assert mapa["trecho"][0]["de"] == (-23.502, -47.450)
        assert mapa["trecho"][0]["para"] == (-23.560, -47.450)

    def test_trecho_e_uma_cadeia_de_caixa_vizinha_em_caixa_vizinha(
        self, organization_a: Organization
    ) -> None:
        """Era uma estrela (montante ligada a cada uma das outras) até 19/09/2026.

        A estrela desenhava pares distantes por construção — em produção, 11 km
        entre as pontas na mediana, contra cabos de 201 m. Em cadeia, cada par é
        o pulo real de uma caixa para a seguinte, e o trecho passa a poder
        seguir o cabo.
        """
        set_current_organization(organization_a)
        self._planta(organization_a)
        # Uma terceira caixa, mais longe ainda: em estrela, ela sairia ligada
        # direto à de cima; em cadeia, sai ligada à do meio.
        NetworkElement.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.CTO, external_id="CTO-XL", name="mais longe",
            latitude=-23.600, longitude=-47.450,
            parent_kind=NetworkElement.Kind.OLT, parent_external_id="OLT-1",
        )
        quedas = [
            _drop(organization_a, login="a", cto="CTO-P", lat=-23.502, lon=-47.450),
            _drop(organization_a, login="b", cto="CTO-L", lat=-23.560, lon=-47.450),
            _drop(organization_a, login="c", cto="CTO-XL", lat=-23.600, lon=-47.450),
        ]
        mapa = compute_mapa(organization_a, quedas)
        assert len(mapa["trecho"]) == 2
        # Começa na mais próxima do POP e segue pela vizinha, não pela distante.
        assert mapa["trecho"][0]["de"] == (-23.502, -47.450)
        assert mapa["trecho"][0]["para"] == (-23.560, -47.450)
        assert mapa["trecho"][1]["de"] == (-23.560, -47.450)
        assert mapa["trecho"][1]["para"] == (-23.600, -47.450)

    def test_uma_caixa_so_nao_tem_trecho(self, organization_a: Organization) -> None:
        self._planta(organization_a)
        quedas = [
            _drop(organization_a, login="a", cto="CTO-P", lat=-23.502, lon=-47.450)
        ]
        assert compute_mapa(organization_a, quedas)["trecho"] == []

    def test_sem_pop_no_cadastro_nao_desenha_nada(
        self, organization_a: Organization
    ) -> None:
        """Sem POP não há como saber quem está a montante. Chutar um sentido
        seria pior que não desenhar."""
        set_current_organization(organization_a)
        NetworkElement.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.CTO, external_id="CTO-S", name="sem pai",
            latitude=-23.502, longitude=-47.450,
        )
        quedas = [
            _drop(organization_a, login="a", cto="CTO-S", lat=-23.502, lon=-47.450),
            _drop(organization_a, login="b", cto="CTO-S", lat=-23.503, lon=-47.451),
        ]
        mapa = compute_mapa(organization_a, quedas)
        assert mapa["ligacoes"] == []
        assert mapa["trecho"] == []

    def test_caixa_vizinha_intacta_entra_como_ponto(
        self, organization_a: Organization
    ) -> None:
        self._planta(organization_a)
        quedas = [
            _drop(organization_a, login="a", cto="CTO-P", lat=-23.502, lon=-47.450)
        ]
        mapa = compute_mapa(
            organization_a,
            quedas,
            vizinhas_intactas=[
                {"cto": "CTO-L", "nome": "longe", "lat": -23.56, "lon": -47.45, "de_pe": 9},
                # Sem coordenada não vira ponto — mas segue na lista do card.
                {"cto": "CTO-X", "nome": "sem geo", "lat": None, "lon": None, "de_pe": 4},
            ],
        )
        assert len(mapa["vizinhas"]) == 1
        assert "9 no ar" in mapa["vizinhas"][0]["label"]

    def test_legenda_avisa_que_a_tracejada_nao_e_o_cabo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "não o caminho da fibra" in html
        # E a cheia, que é o cabo, vem com a ressalva de que é projeto.
        assert "cadastro, não leitura" in html


# =============================================================================
# Reincidência do trecho (R8) — acidente vs. trecho cronicamente ruim
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestReincidencia:
    def test_terceira_massiva_da_mesma_olt_se_declara_terceira(
        self, organization_a: Organization
    ) -> None:
        """O ordinal é o produto: "3ª massiva" é o que separa azar de trecho ruim."""
        set_current_organization(organization_a)
        _outage(organization_a, element_id="1", inicio_min_atras=60 * 24 * 5)
        anterior = _outage(organization_a, element_id="1", inicio_min_atras=60 * 24 * 2)
        atual = _outage(organization_a, element_id="1", inicio_min_atras=30)

        r = compute_reincidencia(
            organization_a, [atual], now=timezone.now()
        )[atual.pk]
        assert r["determinavel"] is True
        assert r["total"] == 3
        assert r["anteriores"] == 2
        assert r["reincidente"] is True
        assert r["ultima_anterior"] == anterior.started_at
        assert r["frase"].startswith("3ª massiva")

    def test_elemento_diferente_nao_soma(self, organization_a: Organization) -> None:
        """A identidade é (escopo, id do elemento). OLT 2 não conta para OLT 1."""
        set_current_organization(organization_a)
        _outage(organization_a, element_id="2", inicio_min_atras=60 * 24)
        atual = _outage(organization_a, element_id="1")
        r = compute_reincidencia(organization_a, [atual], now=timezone.now())[atual.pk]
        assert r["anteriores"] == 0
        assert r["reincidente"] is False
        assert r["frase"].startswith("Primeira massiva")

    def test_massiva_posterior_nao_entra_no_ordinal_da_anterior(
        self, organization_a: Organization
    ) -> None:
        """Olhando uma encerrada no histórico, o que veio DEPOIS dela não conta.

        Senão a massiva de terça viraria "a 3ª" por causa do que aconteceu na
        quinta — o card diria algo que não era verdade quando o evento estava
        acontecendo.
        """
        set_current_organization(organization_a)
        antiga = _outage(organization_a, element_id="1", inicio_min_atras=60 * 24 * 3)
        _outage(organization_a, element_id="1", inicio_min_atras=60)
        r = compute_reincidencia(organization_a, [antiga], now=timezone.now())[antiga.pk]
        assert r["total"] == 1
        assert r["reincidente"] is False

    def test_escopo_sem_elemento_se_recusa_a_contar(
        self, organization_a: Organization
    ) -> None:
        """GEO não tem elemento no cadastro: o cluster muda de forma a cada
        evento, e somar todos num contador só juntaria bairros diferentes."""
        set_current_organization(organization_a)
        _outage(
            organization_a, scope=OutageEvent.Scope.GEO, element_id="",
            element_label="Cluster geográfico", inicio_min_atras=60 * 24,
        )
        atual = _outage(
            organization_a, scope=OutageEvent.Scope.GEO, element_id="",
            element_label="Cluster geográfico",
        )
        r = compute_reincidencia(organization_a, [atual], now=timezone.now())[atual.pk]
        assert r["determinavel"] is False
        assert r["reincidente"] is False
        assert "não tem elemento fixo" in r["motivo"]

    def test_janela_declarada_e_a_do_registro_nao_os_90_dias(
        self, organization_a: Organization
    ) -> None:
        """Com 9 dias de registro, "1 massiva em 90 dias" seria elogio falso a um
        trecho que ninguém observou. A janela efetiva vai escrita na tela."""
        set_current_organization(organization_a)
        atual = _outage(organization_a, element_id="1", inicio_min_atras=60 * 24 * 9)
        r = compute_reincidencia(organization_a, [atual], now=timezone.now())[atual.pk]
        assert r["janela_parcial"] is True
        assert r["dias_observados"] == 9
        assert "9 dias de registro" in r["frase"]
        assert "menos que a janela de 90 dias" in r["ressalva_janela"]

    def test_massiva_fora_da_janela_nao_conta(
        self, organization_a: Organization
    ) -> None:
        """Passou dos 90 dias, sai da conta — e aí a janela nominal é a real."""
        set_current_organization(organization_a)
        _outage(organization_a, element_id="1", inicio_min_atras=60 * 24 * 200)
        atual = _outage(organization_a, element_id="1")
        r = compute_reincidencia(organization_a, [atual], now=timezone.now())[atual.pk]
        assert r["anteriores"] == 0
        assert r["janela_parcial"] is False
        assert r["ressalva_janela"] == ""
        assert "90 dias" in r["frase"]

    def test_card_mostra_a_reincidencia(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        _outage(
            organization_a, element_id="1", ended=True,
            inicio_min_atras=60 * 24 * 2,
        )
        _outage(organization_a, element_id="1", affected=5)
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Reincidência do trecho" in html
        assert "2ª massiva deste elemento" in html

    def test_historico_marca_a_repetida(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        _outage(
            organization_a, element_id="1", ended=True,
            inicio_min_atras=60 * 24 * 2,
        )
        _outage(organization_a, element_id="1", ended=True, inicio_min_atras=120)
        client.force_login(user_a)
        resp = client.get(URL)
        linhas = resp.context["historico"]
        # Ordenado por fim desc: a mais recente é a 2ª ocorrência.
        assert linhas[0]["reincidencia"]["total"] == 2
        assert linhas[1]["reincidencia"]["total"] == 1
        assert "2ª massiva deste elemento" in resp.content.decode()


# =============================================================================
# Cabos candidatos (R5) — destravado pela geometria do spike R2
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCabosCandidatos:
    def _cto(
        self, org: Organization, *, external_id: str, lat: float, lon: float
    ) -> NetworkElement:
        set_current_organization(org)
        return NetworkElement.objects.create(
            organization=org, source_type="IXC", kind=NetworkElement.Kind.CTO,
            external_id=external_id, name=f"CX {external_id}",
            latitude=lat, longitude=lon,
        )

    def _cabo(
        self, org: Organization, *, external_id: str, nome: str,
        pontos: list[list[float]],
    ) -> NetworkElementGeometry:
        set_current_organization(org)
        return NetworkElementGeometry.objects.create(
            organization=org, source_type="IXC", kind=NetworkElement.Kind.CABLE,
            external_id=external_id, name=nome, points=pontos,
        )

    def test_cabo_que_passa_na_caixa_afetada_vira_candidato(
        self, organization_a: Organization
    ) -> None:
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA AS80 12FO BACKBONE 18",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        self._cabo(
            organization_a, external_id="C9", nome="CLIENTE DROP 1FO 30",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        quedas = [_drop(organization_a, login="a1", cto="CTO-1")]

        c = compute_cabos_candidatos(organization_a, quedas)
        assert c["determinavel"] is True
        assert [cabo["external_id"] for cabo in c["cabos"]] == ["C1"]
        assert c["cabos"][0]["classe"] == "BACKBONE"
        assert c["ctos_sem_cabo"] == 0

    def test_sem_caixa_com_coordenada_o_bloco_se_recusa(
        self, organization_a: Organization
    ) -> None:
        """Sem ponto de partida não há distância a medir — e dizer isso é
        diferente de dizer que não há cabo candidato."""
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA BACKBONE 1",
            pontos=[[-23.5, -47.4], [-23.4, -47.4]],
        )
        quedas = [_drop(organization_a, login="a1", cto="CTO-SEM-GEO")]
        c = compute_cabos_candidatos(organization_a, quedas)
        assert c["determinavel"] is False
        assert "coordenada" in c["motivo"]

    def test_sem_tracado_sincronizado_o_bloco_diz_isso(
        self, organization_a: Organization
    ) -> None:
        """Estado diferente do anterior, e com ação diferente: aqui falta rodar
        o sync da geometria, não falta cadastro de caixa."""
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        quedas = [_drop(organization_a, login="a1", cto="CTO-1")]
        c = compute_cabos_candidatos(organization_a, quedas)
        assert c["determinavel"] is False
        assert "sincronizado" in c["motivo"]

    def test_caixa_longe_de_todo_cabo_e_declarada(
        self, organization_a: Organization
    ) -> None:
        """~15% das caixas de produção não têm cabo a menos de 30 m. Sem esta
        contagem, uma lista curta pareceria 'achamos pouco'."""
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        self._cto(organization_a, external_id="CTO-2", lat=-23.6, lon=-47.5)
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA BACKBONE 1",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        quedas = [
            _drop(organization_a, login="a1", cto="CTO-1"),
            _drop(organization_a, login="a2", cto="CTO-2"),
        ]
        c = compute_cabos_candidatos(organization_a, quedas)
        assert c["ctos_com_coordenada"] == 2
        assert c["ctos_sem_cabo"] == 1

    def test_card_mostra_os_candidatos_sem_prometer_rompimento(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        outage = _outage(organization_a, affected=5)
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA AS80 12FO BACKBONE 18",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        for i in range(5):
            drop = _drop(organization_a, login=f"cai{i}", cto="CTO-1")
            OutageAffectedLogin.objects.create(
                organization=organization_a, outage=outage, drop_event=drop,
                login=drop.login, dropped_at=drop.dropped_at,
            )
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Cabos candidatos" in html
        assert "FIBRA AS80 12FO BACKBONE 18" in html
        # A promessa que a tela nunca faz.
        assert "cabo rompido" not in html.lower()

    def test_mapa_desenha_o_tracado_do_candidato(
        self, organization_a: Organization
    ) -> None:
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA BACKBONE 1",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        quedas = [_drop(organization_a, login="a1", cto="CTO-1", lat=-23.5, lon=-47.4)]
        mapa = compute_mapa(organization_a, quedas, cabos_candidatos=["C1"])
        assert len(mapa["cabos"]) == 1
        assert mapa["cabos"][0]["nome"] == "FIBRA BACKBONE 1"
        assert len(mapa["cabos"][0]["pontos"]) == 2

    def test_mapa_sem_candidato_nao_desenha_cabo(
        self, organization_a: Organization
    ) -> None:
        """O mapa desenha os candidatos, não o cadastro inteiro: 1.191 cabos
        seriam um borrão sem pergunta."""
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA BACKBONE 1",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        quedas = [_drop(organization_a, login="a1", cto="CTO-1", lat=-23.5, lon=-47.4)]
        assert compute_mapa(organization_a, quedas)["cabos"] == []

    def test_cabo_sem_classe_no_nome_e_declarado(
        self, organization_a: Organization
    ) -> None:
        """53% dos cabos de produção não dizem a classe no nome ("01FO", "rede
        neutra"). Sem isso escrito, a ausência do selo pareceria afirmação de
        que o cabo não é tronco."""
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        self._cabo(
            organization_a, external_id="C1", nome="01FO",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        quedas = [_drop(organization_a, login="a1", cto="CTO-1")]
        c = compute_cabos_candidatos(organization_a, quedas)
        assert c["cabos"][0]["classe"] == ""
        assert c["sem_classe"] == 1

    def test_emenda_perto_da_massiva_entra_no_mapa(
        self, organization_a: Organization
    ) -> None:
        """A emenda é onde o cabo é aberto — o primeiro lugar que o técnico abre
        quando o trecho passa por ali."""
        set_current_organization(organization_a)
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        NetworkElementGeometry.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.SPLICE, external_id="E1",
            name="Caixa de Emenda Vermelha 142", points=[[-23.5004, -47.4]],
        )
        NetworkElementGeometry.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.SPLICE, external_id="E2",
            name="Emenda de outro bairro", points=[[-23.6, -47.5]],
        )
        quedas = [_drop(organization_a, login="a1", cto="CTO-1", lat=-23.5, lon=-47.4)]

        mapa = compute_mapa(organization_a, quedas)
        assert len(mapa["emendas"]) == 1
        assert "Vermelha 142" in mapa["emendas"][0]["label"]

    def test_trecho_passa_a_seguir_o_cabo_quando_ha_cabo_nas_duas_pontas(
        self, organization_a: Organization
    ) -> None:
        """A reta entre caixas atravessa quarteirão; o cabo faz a curva da rua."""
        set_current_organization(organization_a)
        # Duas caixas afetadas, ambas sobre o mesmo cabo, e o POP que define o
        # sentido do trecho.
        self._cto(organization_a, external_id="CTO-1", lat=-23.5000, lon=-47.4)
        self._cto(organization_a, external_id="CTO-2", lat=-23.5018, lon=-47.4)
        NetworkElement.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.OLT, external_id="OLT-1",
            parent_kind=NetworkElement.Kind.POP, parent_external_id="POP-1",
        )
        for cto in ("CTO-1", "CTO-2"):
            NetworkElement.objects.filter(
                organization=organization_a, external_id=cto
            ).update(parent_kind=NetworkElement.Kind.OLT, parent_external_id="OLT-1")
        NetworkElement.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.POP, external_id="POP-1", name="POP Centro",
            latitude=-23.4990, longitude=-47.4,
        )
        # O cabo passa pelas duas, com um vértice no meio (a curva da rua).
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA AS80 12FO BACKBONE 18",
            pontos=[[-23.5000, -47.4], [-23.5009, -47.4002], [-23.5018, -47.4]],
        )
        quedas = [
            _drop(organization_a, login="a1", cto="CTO-1", lat=-23.5000, lon=-47.4),
            _drop(organization_a, login="a2", cto="CTO-2", lat=-23.5018, lon=-47.4),
        ]

        mapa = compute_mapa(organization_a, quedas, cabos_candidatos=["C1"])
        assert len(mapa["trecho_no_cabo"]) == 1
        # O vértice do meio está no desenho: é ele que faz o trecho seguir a rua.
        assert len(mapa["trecho_no_cabo"][0]["pontos"]) == 3
        assert "pelo FIBRA AS80 12FO BACKBONE 18" in mapa["trecho_no_cabo"][0]["nome"]
        # E a reta tracejada continua lá, dizendo a mesma coisa por baixo.
        assert mapa["trecho"]

    def test_sem_cabo_nas_duas_pontas_so_resta_a_reta(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        self._cto(organization_a, external_id="CTO-1", lat=-23.5, lon=-47.4)
        self._cto(organization_a, external_id="CTO-2", lat=-23.52, lon=-47.42)
        self._cabo(
            organization_a, external_id="C1", nome="FIBRA BACKBONE 1",
            pontos=[[-23.5001, -47.4], [-23.4999, -47.4]],
        )
        quedas = [
            _drop(organization_a, login="a1", cto="CTO-1", lat=-23.5, lon=-47.4),
            _drop(organization_a, login="a2", cto="CTO-2", lat=-23.52, lon=-47.42),
        ]
        mapa = compute_mapa(organization_a, quedas, cabos_candidatos=["C1"])
        assert mapa["trecho_no_cabo"] == []


# ---------------------------------------------------------------------------
# Ícones e filtros do mapa (21/09/2026)
# ---------------------------------------------------------------------------
class TestIconesEFiltrosDoMapa:
    """O mapa passou a ter ícone por camada e caixa de ligar/desligar.

    Dois riscos que estes testes travam:

    1. **emoji não aparece no mapa.** O motor por baixo do `scattermap` é o
       MapLibre, que desenha texto com os glifos do style — e o protocolo de
       glifos só cobre pontos de código até U+FFFF. Uma casinha emoji
       (U+1F3E0) sairia como espaço em branco em produção, e o defeito só
       apareceria no olho de quem abrisse a tela;
    2. **filtro sem grupo não filtra.** O JS liga e desliga camadas lendo
       `meta.grupo` de cada traço; um traço sem grupo fica preso na tela.
    """

    def _mapa(self) -> dict[str, Any]:
        return {
            "clientes": [{"lat": -23.5, "lon": -47.4, "label": "a1 · caiu 10:00"}],
            "voltaram": [{"lat": -23.51, "lon": -47.41, "label": "a2 · voltou 10:20"}],
            "ctos": [{"lat": -23.5, "lon": -47.4, "label": "CTO-1"}],
            "vizinhas": [],
            "emendas": [{"lat": -23.505, "lon": -47.405, "label": "CE-1"}],
            "pops": [{"lat": -23.4, "lon": -47.3, "label": "POP Centro"}],
            "ligacoes": [{"de": (-23.5, -47.4), "para": (-23.4, -47.3), "label": "x"}],
            "trecho": [{"de": (-23.5, -47.4), "para": (-23.51, -47.41), "label": "y"}],
            "cabos": [{"nome": "FIBRA 12FO", "pontos": [(-23.5, -47.4), (-23.51, -47.41)]}],
            "trecho_no_cabo": [],
        }

    def _traces(self) -> list[dict[str, Any]]:
        import json

        from apps.dashboards.charts import outage_map

        return json.loads(outage_map(self._mapa()))["data"]

    def test_todo_traco_declara_seu_grupo(self) -> None:
        for trace in self._traces():
            assert (trace.get("meta") or {}).get("grupo"), trace.get("name")

    def test_icones_ficam_no_plano_basico_do_unicode(self) -> None:
        """Nenhum caractere acima de U+FFFF — o MapLibre não os desenha."""
        from apps.dashboards import charts

        for icone in (
            charts._ICONE_CLIENTE,
            charts._ICONE_CAIXA,
            charts._ICONE_EMENDA,
            charts._ICONE_POP,
        ):
            assert len(icone) == 1
            assert ord(icone) <= 0xFFFF

    def test_cliente_sai_com_casa_e_o_rotulo_vai_para_o_hover(self) -> None:
        from apps.dashboards import charts

        clientes = next(
            t for t in self._traces() if t.get("name", "").endswith("Fora agora")
        )
        # O glifo ocupa o `text`; sem o `hovertext`, o hover diria "⌂".
        assert clientes["text"] == [charts._ICONE_CLIENTE]
        assert clientes["hovertext"] == ["a1 · caiu 10:00"]

    def test_pop_e_ligacao_logica_comecam_desligados(self) -> None:
        """Linhas longas que cruzam o mapa e não são destino de ninguém.

        Não somem: viram caixa desmarcada acima do mapa.
        """
        por_grupo = {
            t["meta"]["grupo"]: t.get("visible", True) for t in self._traces()
        }
        assert por_grupo["pop"] is False
        assert por_grupo["fora"] is True
        assert por_grupo["caixas"] is True
        assert por_grupo["cabo"] is True

    def test_pop_desligado_nao_estica_o_enquadramento(self) -> None:
        """Com o POP na conta, o evento virava um punhado de pixels."""
        import json

        from apps.dashboards.charts import outage_map

        centro = json.loads(outage_map(self._mapa()))["layout"]["map"]["center"]
        # O POP está a -23.4; o enquadramento fica no quarteirão do evento.
        assert centro["lat"] < -23.49

    def test_a_tela_traz_as_caixas_de_filtro_e_o_foco_no_tecnico(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        for grupo in ("fora", "voltaram", "caixas", "cabo", "pop"):
            assert f'data-grupo="{grupo}"' in html
        assert 'data-preset="tecnico"' in html
        assert "Foco no técnico" in html


# ---------------------------------------------------------------------------
# Filtros da tabela de clientes (21/09/2026)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestFiltrosDaTabelaDeClientes:
    """Quem voltou, quem não voltou, e quem está abaixo de -25 dBm.

    O limiar absoluto (-25 dBm, dado pelo operador) convive com o alerta
    relativo de 3 dB e responde outra pergunta: o relativo diz *piorou neste
    reparo*, o absoluto diz *está ruim*. Uma ONU que sempre esteve a -27 não
    acende o relativo, e é quem precisa de visita.

    A armadilha que os testes travam: **sem leitura não é saudável**. Se a
    ausência de sinal contasse como "acima do limiar", a tela diria que está
    tudo bem num cliente que ninguém mediu.
    """

    def test_abaixo_do_limiar_e_marcado_pela_leitura_de_retorno(self) -> None:
        celula = _signal_cell(
            SimpleNamespace(signal_rx_before=-23.0, signal_rx_after=-27.0)  # type: ignore[arg-type]
        )
        assert celula["critico"] is True
        assert celula["critico_de"] == "retorno"

    def test_sem_retorno_o_limiar_julga_a_base(self) -> None:
        celula = _signal_cell(
            SimpleNamespace(signal_rx_before=-26.2)  # type: ignore[arg-type]
        )
        assert celula["critico"] is True
        assert celula["critico_de"] == "base"

    def test_acima_do_limiar_nao_e_marcado(self) -> None:
        celula = _signal_cell(
            SimpleNamespace(signal_rx_before=-22.0, signal_rx_after=-24.9)  # type: ignore[arg-type]
        )
        assert celula["critico"] is False

    def test_sem_leitura_nao_e_saudavel_e_nem_critico(self) -> None:
        celula = _signal_cell(SimpleNamespace())  # type: ignore[arg-type]
        assert celula["critico"] is False
        assert celula["sem_leitura"] is True

    @pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
    def test_a_tela_conta_cada_recorte_e_separa_quem_nao_tem_leitura(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, affected=3, restored=1)
        casos = (
            # login,     voltou, antes,  depois
            ("ruim", True, -23.0, -27.4),
            ("bom", False, -21.0, -21.5),
            ("sem-leitura", False, None, None),
        )
        for login, voltou, antes, depois in casos:
            drop = _drop(organization_a, login=login, restored=voltou)
            if antes is not None:
                drop.signal_rx_before = antes
                drop.signal_rx_after = depois
                drop.save(update_fields=["signal_rx_before", "signal_rx_after"])
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
        filtros = resp.context["filtros_clientes"]
        assert filtros["total"] == 3
        assert filtros["voltaram"] == 1
        assert filtros["fora"] == 2
        assert filtros["criticos"] == 1
        # O não medido é contado à parte — não some dentro de "acima do limiar".
        assert filtros["sem_leitura"] == 1

        html = resp.content.decode()
        for status in ("todos", "fora", "voltaram"):
            assert f'data-status="{status}"' in html
        assert 'id="filtro-sinal-critico"' in html
        # A linha carrega o recorte: é por estes atributos que o filtro anda.
        assert 'data-critico="1"' in html
        assert "sem leitura óptica" in html
