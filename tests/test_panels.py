"""Painéis de parede: pareamento, credencial e frescor.

O painel é a primeira tela do sistema que **não tem usuário** — uma TV pareada,
ligada meses, sem ninguém para fazer login. Isso cria riscos que nenhuma outra
tela tem, e é o que estes testes travam:

1. **o que aparece na TV não dá acesso.** O QR e o código de pareamento são
   públicos por natureza (qualquer um na sala fotografa); a credencial é
   entregue por outro canal, ao navegador que pediu o pareamento;
2. **a credencial é de um painel e de uma organização.** TV do NOC não abre o
   executivo, e TV de uma empresa não vê dado de outra;
3. **revogar funciona de verdade** — credencial que não se revoga é eterna, e TV
   some, muda de sala e é roubada;
4. **o painel não mente sobre a idade do dado.** Quando não sabe, diz "—" em vez
   de zero, que é a mentira mais confortável de uma tela de parede.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.dashboards.panels import get_panel
from apps.dashboards.panels.alerts import (
    NIVEL_ATENCAO,
    NIVEL_CRITICO,
    NIVEL_INFO,
    classificar,
    deve_interromper,
    nivel_da_massiva,
)
from apps.dashboards.panels.atendimento import tem_atendimento
from apps.dashboards.panels.pairing import (
    COOKIE_NOME,
    hash_segredo,
    novo_codigo,
    novo_segredo,
)
from apps.network.infrastructure.models import OutageEvent
from apps.shared.context import set_current_organization
from apps.tenancy.models import (
    AccessGroup,
    DisplayDevice,
    Organization,
    OrganizationMembership,
    User,
)

PAIR_URL = "/paineis/noc/parear/"
STATUS_URL = "/paineis/noc/parear/status/"
PANEL_URL = "/paineis/noc/"
SNAPSHOT_URL = "/paineis/noc/snapshot/"
APPROVE_URL = "/paineis/aprovar/"


def _tv_pareada(org: Organization, *, panel: str = "noc") -> str:
    """Cria uma TV já aprovada e devolve a credencial em claro."""
    token = novo_segredo()
    DisplayDevice.objects.create(
        panel_key=panel,
        code=f"C{org.pk}{panel[:3]}",
        device_token_hash=hash_segredo(novo_segredo()),
        display_token_hash=hash_segredo(token),
        organization=org,
        approved_at=timezone.now(),
    )
    return token


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPareamento:
    def test_tela_de_pareamento_mostra_codigo_e_nao_credencial(
        self, client: Any, organization_a: Organization
    ) -> None:
        resp = client.get(PAIR_URL)
        assert resp.status_code == 200
        device = DisplayDevice.objects.get()
        html = resp.content.decode()
        # O código aparece; a credencial não existe ainda, e o segredo do
        # dispositivo só vive no cookie.
        assert device.code in html
        assert device.display_token_hash == ""
        assert f"{COOKIE_NOME}_pair" in resp.cookies
        assert resp.cookies[f"{COOKIE_NOME}_pair"].value not in html

    def test_qr_aponta_para_a_aprovacao_com_o_codigo(
        self, client: Any, organization_a: Organization
    ) -> None:
        resp = client.get(PAIR_URL)
        html = resp.content.decode()
        assert "<svg" in html  # QR renderizado no servidor, sem serviço externo
        assert "/paineis/aprovar/" in html

    def test_status_sem_pedido_nao_vaza_credencial(
        self, client: Any, organization_a: Organization
    ) -> None:
        resp = client.get(STATUS_URL)
        assert resp.status_code == 400
        assert COOKIE_NOME not in resp.cookies

    def test_aprovacao_exige_login(self, client: Any, organization_a: Organization) -> None:
        client.get(PAIR_URL)
        device = DisplayDevice.objects.get()
        resp = client.get(f"{APPROVE_URL}?code={device.code}")
        assert resp.status_code == 302
        assert "/accounts/login" in resp["Location"] or "login" in resp["Location"]

    def test_fluxo_completo_entrega_credencial_a_quem_pediu(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """O caminho feliz inteiro — e o ponto é QUEM recebe a credencial."""
        tv = Client()
        tv.get(PAIR_URL)
        device = DisplayDevice.objects.get()

        # Outra pessoa, noutro navegador, aprova depois de logar.
        celular = Client()
        celular.force_login(user_a)
        resp = celular.post(APPROVE_URL, {"code": device.code, "name": "TV da bancada"})
        assert resp.status_code == 200
        device.refresh_from_db()
        assert device.organization_id == organization_a.pk
        assert device.approved_by_id == user_a.pk
        assert device.name == "TV da bancada"
        # O navegador que aprovou NÃO recebe credencial de display.
        assert COOKIE_NOME not in resp.cookies

        # A TV, sim — na consulta de status dela.
        resp = tv.get(STATUS_URL)
        assert resp.json()["estado"] == "pareado"
        assert COOKIE_NOME in resp.cookies
        # E o que foi gravado é o hash, não o segredo.
        device.refresh_from_db()
        assert device.display_token_hash == hash_segredo(resp.cookies[COOKIE_NOME].value)

    def test_codigo_morre_ao_ser_usado(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        tv = Client()
        tv.get(PAIR_URL)
        device = DisplayDevice.objects.get()
        codigo = device.code

        celular = Client()
        celular.force_login(user_a)
        celular.post(APPROVE_URL, {"code": codigo})
        tv.get(STATUS_URL)

        # Uma segunda tentativa com o mesmo código não encontra nada.
        resp = celular.get(f"{APPROVE_URL}?code={codigo}")
        assert "não encontrado" in resp.content.decode().lower()

    def test_codigo_expirado_nao_pareia(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Cinco minutos: a tela de uma TV fica exposta a quem passa na sala."""
        client.get(PAIR_URL)
        device = DisplayDevice.objects.get()
        DisplayDevice.objects.filter(pk=device.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        celular = Client()
        celular.force_login(user_a)
        resp = celular.post(APPROVE_URL, {"code": device.code})
        assert "expirou" in resp.content.decode()
        device.refresh_from_db()
        assert device.approved_at is None

    def test_quem_nao_tem_a_aba_nao_aprova(
        self, client: Any, organization_a: Organization
    ) -> None:
        """A TV não pode ganhar acesso que quem a aprovou não tem."""
        client.get(PAIR_URL)
        device = DisplayDevice.objects.get()
        grupo = AccessGroup.objects.create(
            organization=organization_a, name="Financeiro", allowed_pages=["revenue"]
        )
        user = User.objects.create_user(email="fin@acme.com")
        OrganizationMembership.objects.create(
            user=user, organization=organization_a,
            role=OrganizationMembership.Role.MEMBER, is_active=True, access_group=grupo,
        )
        celular = Client()
        celular.force_login(user)
        resp = celular.post(APPROVE_URL, {"code": device.code})
        assert "não tem acesso" in resp.content.decode()
        device.refresh_from_db()
        assert device.approved_at is None


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCredencialDeDisplay:
    def test_tv_pareada_abre_o_painel_sem_login(
        self, organization_a: Organization
    ) -> None:
        token = _tv_pareada(organization_a)
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        assert tv.get(PANEL_URL).status_code == 200

    def test_sem_credencial_vai_para_o_pareamento(self, client: Any) -> None:
        """TV recém-ligada não tem como 'clicar em entrar'."""
        resp = client.get(PANEL_URL)
        assert resp.status_code == 302
        assert "/parear/" in resp["Location"]

    def test_credencial_de_um_painel_nao_abre_outro(
        self, organization_a: Organization
    ) -> None:
        """Senão a TV da sala de operação amanhece mostrando faturamento."""
        token = _tv_pareada(organization_a, panel="executivo")
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        resp = tv.get(PANEL_URL)
        assert resp.status_code == 302
        assert "/parear/" in resp["Location"]

    def test_revogada_perde_o_acesso(self, organization_a: Organization) -> None:
        token = _tv_pareada(organization_a)
        DisplayDevice.objects.update(revoked_at=timezone.now(), display_token_hash="")
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        assert tv.get(PANEL_URL).status_code == 302

    def test_snapshot_responde_403_em_json_e_nao_redireciona(
        self, client: Any
    ) -> None:
        """Um 302 para o login viraria painel congelado sem explicação na parede."""
        resp = client.get(SNAPSHOT_URL)
        assert resp.status_code == 403
        assert resp.json()["erro"] == "sem_acesso"

    def test_snapshot_da_tv_carimba_o_ultimo_contato(
        self, organization_a: Organization
    ) -> None:
        """É o que permite saber que uma TV parou de aparecer."""
        token = _tv_pareada(organization_a)
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        assert tv.get(SNAPSHOT_URL).status_code == 200
        assert DisplayDevice.objects.get().last_seen_at is not None

    def test_revogacao_e_so_do_dono(
        self, client: Any, user_a: User, organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        _tv_pareada(organization_b)
        alheia = DisplayDevice.objects.get()
        client.force_login(user_a)
        resp = client.post(f"/paineis/dispositivos/{alheia.pk}/revogar/")
        assert resp.status_code == 404
        alheia.refresh_from_db()
        assert alheia.revoked_at is None


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestFrescorEConteudo:
    def test_snapshot_diz_que_nao_sabe_a_idade_em_vez_de_dizer_zero(
        self, organization_a: Organization
    ) -> None:
        """Sem poll nenhum, a idade é desconhecida. Zero pareceria dado fresco —
        a mentira mais confortável de uma tela de parede."""
        token = _tv_pareada(organization_a)
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        dados = tv.get(SNAPSHOT_URL).json()
        assert dados["idade_conhecida"] is False
        assert dados["idade_segundos"] is None
        assert dados["dado_velho"] is True

    def test_sem_massiva_nao_ha_pagina_de_massiva(
        self, organization_a: Organization
    ) -> None:
        """Slide sem pergunta treina a sala a ignorar a TV.

        Desde o T5 a massiva não é um slide de lista e outro de mapa: é **uma
        página por evento**, montada a partir da lista do snapshot. Sem evento,
        nenhuma página.
        """
        panel = get_panel("noc")
        assert panel is not None
        visiveis = [s.key for s in panel.visible_slides({"massivas": []})]
        assert "massiva" not in visiveis
        com_evento = [s.key for s in panel.visible_slides({"massivas": [{"id": 1}]})]
        assert "massiva" in com_evento

    def test_uma_pagina_por_massiva(self, organization_a: Organization) -> None:
        """Com três massivas abertas, três páginas — cada uma com seu mapa.

        O mapa geral respondia "onde estão as massivas"; com três no ar,
        ninguém sabia qual ponto era de qual.
        """
        panel = get_panel("noc")
        assert panel is not None
        snapshot = {"massivas": [{"id": 1}, {"id": 2}, {"id": 3}], "timeline": []}
        paginas = [p for p in panel.paginas(snapshot) if p["spec"].key == "massiva"]
        assert len(paginas) == 3
        # `dom_id` distinto: dois gráficos não podem disputar o mesmo elemento.
        assert len({p["dom_id"] for p in paginas}) == 3

    def test_linha_do_tempo_zerada_sai_da_rotacao(
        self, organization_a: Organization
    ) -> None:
        """A linha do tempo passa a maior parte do dia zerada — que é o estado
        normal da rede, e não algo para ocupar a parede (T4)."""
        panel = get_panel("noc")
        assert panel is not None
        vazia = [s.key for s in panel.visible_slides({"massivas": [], "timeline": []})]
        assert "ultimas24h" not in vazia
        com_queda = [
            s.key
            for s in panel.visible_slides(
                {"massivas": [], "timeline": [{"fora": 3, "voltaram": 0}]}
            )
        ]
        assert "ultimas24h" in com_queda

    def test_a_tela_do_painel_declara_a_idade_e_a_coleta(
        self, organization_a: Organization
    ) -> None:
        token = _tv_pareada(organization_a)
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        html = tv.get(PANEL_URL).content.decode()
        assert "Idade do dado" in html
        assert "Coleta" in html
        assert "Dado velho" in html


# =============================================================================
# P6 — alertas sem fadiga
# =============================================================================
def _linha(**kwargs: Any) -> dict[str, Any]:
    base = {
        "id": 1,
        "scope": "CTO",
        "affected_count": 5,
        "ainda_fora": 5,
        "started_at": timezone.now() - timedelta(minutes=30),
        "is_expected": False,
        "acknowledged_at": None,
    }
    base.update(kwargs)
    return base


class TestSeveridade:
    def test_olt_e_pop_sao_criticos_por_escopo(self) -> None:
        """Uma OLT fora é evento de infraestrutura, independentemente de quantos
        clientes o detector já viu."""
        assert nivel_da_massiva(_linha(scope="OLT"), base_de_clientes=8000) == NIVEL_CRITICO
        assert nivel_da_massiva(_linha(scope="POP"), base_de_clientes=8000) == NIVEL_CRITICO

    def test_fracao_da_base_sobe_o_nivel(self) -> None:
        assert nivel_da_massiva(
            _linha(affected_count=400), base_de_clientes=8000
        ) == NIVEL_CRITICO  # 5%
        assert nivel_da_massiva(
            _linha(affected_count=80), base_de_clientes=8000
        ) == NIVEL_ATENCAO  # 1%
        assert nivel_da_massiva(
            _linha(affected_count=5), base_de_clientes=8000
        ) == NIVEL_INFO

    def test_manutencao_programada_nunca_passa_de_info(self) -> None:
        """Alarmar o que foi avisado é o caminho mais curto para a equipe parar
        de olhar a tela."""
        linha = _linha(scope="OLT", affected_count=500, is_expected=True)
        assert nivel_da_massiva(linha, base_de_clientes=8000) == NIVEL_INFO

    def test_sem_base_conhecida_nao_inventa_fracao(self) -> None:
        assert nivel_da_massiva(_linha(affected_count=500), base_de_clientes=0) == NIVEL_INFO


class TestInterrupcao:
    def test_so_critico_toma_a_tela(self) -> None:
        linha = _linha(nivel=NIVEL_ATENCAO)
        assert deve_interromper(linha, agora=timezone.now()) is False

    def test_evento_jovem_nao_interrompe(self) -> None:
        """Supressão por persistência: mata flap e reboot de OLT, que se
        resolvem sozinhos antes disso."""
        linha = _linha(nivel=NIVEL_CRITICO, started_at=timezone.now() - timedelta(minutes=2))
        assert deve_interromper(linha, agora=timezone.now()) is False

    def test_evento_persistente_interrompe(self) -> None:
        linha = _linha(nivel=NIVEL_CRITICO, started_at=timezone.now() - timedelta(minutes=8))
        assert deve_interromper(linha, agora=timezone.now()) is True

    def test_reconhecida_para_de_interromper(self) -> None:
        """Alguém assumiu; insistir transforma o painel em barulho."""
        linha = _linha(
            nivel=NIVEL_CRITICO,
            started_at=timezone.now() - timedelta(minutes=30),
            acknowledged_at=timezone.now(),
        )
        assert deve_interromper(linha, agora=timezone.now()) is False

    def test_classificar_escolhe_a_mais_grave_e_maior(self) -> None:
        agora = timezone.now()
        antiga = timezone.now() - timedelta(minutes=30)
        linhas = [
            _linha(id=1, scope="OLT", ainda_fora=10, started_at=antiga),
            _linha(id=2, scope="POP", ainda_fora=80, started_at=antiga),
            _linha(id=3, scope="CTO", ainda_fora=200, started_at=antiga),
        ]
        alerta = classificar(linhas, base_de_clientes=8000, agora=agora)
        assert alerta["takeover"]["id"] == 2
        assert alerta["por_nivel"][NIVEL_CRITICO] == 2

    def test_sem_evento_critico_nao_ha_takeover(self) -> None:
        linhas = [_linha(scope="CTO", affected_count=5)]
        alerta = classificar(linhas, base_de_clientes=8000, agora=timezone.now())
        assert alerta["takeover"] is None


# =============================================================================
# P8 — reconhecimento
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestReconhecimento:
    def _massiva_aberta(self, org: Organization) -> Any:
        set_current_organization(org)
        return OutageEvent.objects.create(
            organization=org,
            started_at=timezone.now() - timedelta(minutes=20),
            last_detected_at=timezone.now(),
            scope=OutageEvent.Scope.OLT,
            element_external_id="1",
            element_label="OLT 1",
            confidence=OutageEvent.Confidence.HIGH,
            affected_count=40,
        )

    def test_ciente_grava_quem_assumiu(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = self._massiva_aberta(organization_a)
        client.force_login(user_a)
        resp = client.post(f"/operations/massivas/{outage.pk}/ciente/")
        assert resp.status_code == 302
        outage.refresh_from_db()
        assert outage.acknowledged_by_id == user_a.pk
        assert outage.is_acknowledged is True

    def test_primeiro_a_assumir_e_quem_fica(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Sobrescrever apagaria quem realmente pegou o evento."""
        outage = self._massiva_aberta(organization_a)
        client.force_login(user_a)
        client.post(f"/operations/massivas/{outage.pk}/ciente/")
        outage.refresh_from_db()
        primeiro = outage.acknowledged_at

        segundo = User.objects.create_user(email="segundo@acme.com")
        OrganizationMembership.objects.create(
            user=segundo, organization=organization_a,
            role=OrganizationMembership.Role.MEMBER, is_active=True,
        )
        outro = Client()
        outro.force_login(segundo)
        outro.post(f"/operations/massivas/{outage.pk}/ciente/")

        outage.refresh_from_db()
        assert outage.acknowledged_by_id == user_a.pk
        assert outage.acknowledged_at == primeiro

    def test_next_externo_e_ignorado(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """O link chega de um celular na rua: `next` absoluto viraria redirect
        aberto."""
        outage = self._massiva_aberta(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"/operations/massivas/{outage.pk}/ciente/",
            {"next": "https://exemplo-malicioso.com/"},
        )
        assert resp["Location"] == "/operations/massivas/"

    def test_massiva_de_outra_org_responde_404(
        self, client: Any, user_a: User, organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        alheia = self._massiva_aberta(organization_b)
        set_current_organization(organization_a)
        client.force_login(user_a)
        resp = client.post(f"/operations/massivas/{alheia.pk}/ciente/")
        assert resp.status_code == 404
        alheia.refresh_from_db()
        assert alheia.acknowledged_at is None


# =============================================================================
# P10 — o slide de atendimento
# =============================================================================
@pytest.mark.django_db
class TestSlideDeAtendimento:
    def _atendimento(self, org: Organization, *, minutos_atras: int, status: str) -> Any:
        from apps.atendimento.infrastructure.models import Atendimento

        set_current_organization(org)
        return Atendimento.objects.create(
            organization=org,
            source_type="OPA",
            external_id=f"at-{minutos_atras}-{status}",
            customer_external_id="c1",
            status=status,
            opened_at=timezone.now() - timedelta(minutes=minutos_atras),
        )

    def test_sem_atendimento_sincronizado_o_slide_some(
        self, organization_a: Organization
    ) -> None:
        """Três zeros numa TV se leem como 'está tudo calmo', que é o oposto de
        'não sei'."""
        from apps.dashboards.panels.atendimento import snapshot_atendimento

        # O snapshot roda com a org no contexto (é o que a view faz antes de
        # chamar o painel); aqui o ponto é a ORG SEM atendimento nenhum.
        set_current_organization(organization_a)
        dados = snapshot_atendimento(organization_a, timezone.now())
        assert dados["disponivel"] is False
        assert tem_atendimento({"atendimento": dados}) is False

    def test_fila_conta_abertos_e_em_atendimento_da_janela(
        self, organization_a: Organization
    ) -> None:
        from apps.atendimento.infrastructure.models import Atendimento
        from apps.dashboards.panels.atendimento import snapshot_atendimento

        self._atendimento(organization_a, minutos_atras=10, status=Atendimento.Status.OPEN)
        self._atendimento(
            organization_a, minutos_atras=45, status=Atendimento.Status.IN_PROGRESS
        )
        self._atendimento(
            organization_a, minutos_atras=90, status=Atendimento.Status.CLOSED
        )

        dados = snapshot_atendimento(organization_a, timezone.now())
        assert dados["disponivel"] is True
        assert dados["na_fila"] == 2
        # A espera é a do mais antigo da fila, não a do fechado.
        assert 44 <= dados["espera_minutos"] <= 46
        assert dados["espera_alerta"] is True
        assert dados["parados"] == 0

    def test_conversa_aberta_ha_meses_nao_e_fila(
        self, organization_a: Organization
    ) -> None:
        """O erro que a verificação em produção pegou: 826 "na fila" com 122
        dias de espera eram 813 conversas que ninguém fechou na origem. Isso não
        é gente esperando — é resíduo de cadastro, e numa TV vira paisagem."""
        from apps.atendimento.infrastructure.models import Atendimento
        from apps.dashboards.panels.atendimento import snapshot_atendimento

        self._atendimento(
            organization_a, minutos_atras=60 * 24 * 120, status=Atendimento.Status.IN_PROGRESS
        )
        self._atendimento(organization_a, minutos_atras=20, status=Atendimento.Status.OPEN)

        dados = snapshot_atendimento(organization_a, timezone.now())
        assert dados["na_fila"] == 1
        assert dados["parados"] == 1
        # A espera é a da fila real, não a da conversa de quatro meses atrás.
        assert dados["espera_minutos"] <= 25

    def test_sem_conversa_nova_o_slide_avisa_que_fala_da_coleta(
        self, organization_a: Organization
    ) -> None:
        """"0 na fila" com o sync parado significa "não estamos enxergando",
        não "está calmo" — a diferença que o painel existe para não apagar.

        Medido em produção em 19/09/2026: o último sync do Opa tinha rodado 1,5
        dia antes.
        """
        from apps.atendimento.infrastructure.models import Atendimento
        from apps.dashboards.panels.atendimento import snapshot_atendimento

        self._atendimento(
            organization_a, minutos_atras=60 * 48, status=Atendimento.Status.CLOSED
        )
        dados = snapshot_atendimento(organization_a, timezone.now())
        assert dados["na_fila"] == 0
        assert dados["dado_velho"] is True
        assert dados["idade_horas"] >= 47


# =============================================================================
# T4 — avanço manual e slide vazio
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAvancoManual:
    def test_a_tela_responde_a_clique_e_a_seta(
        self, organization_a: Organization
    ) -> None:
        """Quem está de pé na frente da TV quer passar para a próxima página,
        não esperar 20 s. Controle remoto manda seta; teclado de bancada, espaço."""
        token = _tv_pareada(organization_a)
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        html = tv.get(PANEL_URL).content.decode()
        assert 'addEventListener("click"' in html
        assert "ArrowRight" in html


# =============================================================================
# Ação pelo controle da TV — reconhecer e dizer a causa
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAcaoPeloControle:
    """A TV pode marcar "estou tratando" e a causa (pedido de 21/09/2026).

    Quem age é o **dispositivo**, não uma pessoa: o controle remoto não faz
    login. Por isso o autor gravado é o nome da TV — fingir autoria seria pior
    que não ter, e saber que foi alguém na sala já muda a ação de quem chega
    depois.
    """

    def _massiva(self, org: Organization) -> OutageEvent:
        set_current_organization(org)
        agora = timezone.now()
        return OutageEvent.objects.create(
            organization=org,
            started_at=agora - timedelta(minutes=20),
            last_detected_at=agora,
            scope=OutageEvent.Scope.OLT,
            element_external_id="1",
            element_label="OLT 1",
            confidence=OutageEvent.Confidence.HIGH,
            affected_count=40,
        )

    def _tv(self, org: Organization, nome: str = "TV da bancada") -> Client:
        token = novo_segredo()
        DisplayDevice.objects.create(
            # Código único por TV: duas na mesma sala é o caso normal.
            panel_key="noc", code=novo_codigo(), name=nome,
            device_token_hash=hash_segredo(novo_segredo()),
            display_token_hash=hash_segredo(token),
            organization=org, approved_at=timezone.now(),
        )
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        return tv

    def test_ok_marca_em_tratamento_com_o_nome_da_tv(
        self, organization_a: Organization
    ) -> None:
        outage = self._massiva(organization_a)
        tv = self._tv(organization_a)
        resp = tv.post(f"/paineis/noc/massiva/{outage.pk}/acao/", {"acao": "ciente"})
        assert resp.status_code == 200
        outage.refresh_from_db()
        assert outage.is_acknowledged is True
        assert outage.acknowledged_by_display == "TV da bancada"
        # Sem usuário: o controle não faz login, e inventar autor seria pior.
        assert outage.acknowledged_by_id is None

    def test_numero_registra_a_causa(self, organization_a: Organization) -> None:
        """Quem está na sala às vezes já sabe ("é rompimento") antes de o evento
        encerrar — a fila da aba continua cobrando só as que encerraram sem
        resposta."""
        outage = self._massiva(organization_a)
        tv = self._tv(organization_a)
        resp = tv.post(
            f"/paineis/noc/massiva/{outage.pk}/acao/",
            {"causa": OutageEvent.Cause.ROMPIMENTO.value},
        )
        assert resp.status_code == 200
        outage.refresh_from_db()
        assert outage.confirmed_cause == OutageEvent.Cause.ROMPIMENTO.value
        assert "marcado na TV da bancada" in outage.cause_note

    def test_causa_invalida_e_recusada(self, organization_a: Organization) -> None:
        outage = self._massiva(organization_a)
        tv = self._tv(organization_a)
        resp = tv.post(f"/paineis/noc/massiva/{outage.pk}/acao/", {"causa": "METEORO"})
        assert resp.status_code == 400
        outage.refresh_from_db()
        assert outage.confirmed_cause == ""

    def test_primeiro_a_assumir_continua_sendo_quem_fica(
        self, organization_a: Organization
    ) -> None:
        outage = self._massiva(organization_a)
        tv = self._tv(organization_a)
        tv.post(f"/paineis/noc/massiva/{outage.pk}/acao/", {"acao": "ciente"})
        outage.refresh_from_db()
        primeiro = outage.acknowledged_at

        outra = self._tv(organization_a, nome="TV da diretoria")
        outra.post(f"/paineis/noc/massiva/{outage.pk}/acao/", {"acao": "ciente"})
        outage.refresh_from_db()
        assert outage.acknowledged_at == primeiro
        assert outage.acknowledged_by_display == "TV da bancada"

    def test_sem_credencial_nao_age(self, organization_a: Organization) -> None:
        outage = self._massiva(organization_a)
        resp = Client().post(
            f"/paineis/noc/massiva/{outage.pk}/acao/", {"acao": "ciente"}
        )
        assert resp.status_code == 403
        outage.refresh_from_db()
        assert outage.acknowledged_at is None

    def test_tv_de_outra_org_nao_alcanca_a_massiva(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        alheia = self._massiva(organization_b)
        set_current_organization(organization_a)
        tv = self._tv(organization_a)
        resp = tv.post(f"/paineis/noc/massiva/{alheia.pk}/acao/", {"acao": "ciente"})
        assert resp.status_code == 404
        alheia.refresh_from_db()
        assert alheia.acknowledged_at is None


# ---------------------------------------------------------------------------
# Mapa do dia (21/09/2026)
# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestMapaDoDia:
    """A TV só tinha mapa dentro de uma massiva aberta; com a rede calma, nenhum.

    O desenho saiu de uma medição em produção: 24 h são ~3.000 quedas, 99% já
    restauradas. Três mil pontos verdes não são um mapa. Então a unidade aqui é
    a **caixa**, com tamanho conforme o estrago — e a caixa de uma queda só fica
    de fora, porque uma queda isolada em 24 h é o ruído normal da operação.
    """

    def _cto(self, org: Organization, external_id: str) -> Any:
        from apps.network.infrastructure.models import NetworkElement

        return NetworkElement.objects.create(
            organization=org,
            source_type="IXC",
            kind=NetworkElement.Kind.CTO,
            external_id=external_id,
            name=f"CTO {external_id}",
            latitude=-23.5,
            longitude=-47.4,
        )

    def _queda(
        self,
        org: Organization,
        *,
        login: str,
        cto: str,
        horas_atras: float = 2,
        restaurada: bool = True,
    ) -> Any:
        from apps.network.infrastructure.models import Connection, ConnectionDropEvent

        agora = timezone.now()
        conn = Connection.objects.create(
            organization=org,
            source_type="IXC",
            external_id=f"conn-{login}",
            customer_external_id=f"cust-{login}",
            login=login,
            status=Connection.Status.OFFLINE,
        )
        return ConnectionDropEvent.objects.create(
            organization=org,
            connection=conn,
            login=login,
            dropped_at=agora - timedelta(hours=horas_atras),
            restored_at=agora - timedelta(hours=horas_atras - 1) if restaurada else None,
            cto_external_id=cto,
        )

    def test_agrupa_por_caixa_e_conta_quem_segue_fora(
        self, organization_a: Organization
    ) -> None:
        from apps.dashboards.massivas import compute_mapa_do_dia

        set_current_organization(organization_a)
        self._cto(organization_a, "CTO-1")
        self._queda(organization_a, login="a1", cto="CTO-1")
        self._queda(organization_a, login="a2", cto="CTO-1", restaurada=False)

        mapa = compute_mapa_do_dia(organization_a, now=timezone.now())
        assert len(mapa["pontos"]) == 1
        ponto = mapa["pontos"][0]
        assert ponto["quedas"] == 2
        assert ponto["fora"] == 1
        assert "2 quedas em 24h" in ponto["label"]
        assert "1 ainda fora" in ponto["label"]

    def test_caixa_de_uma_queda_so_nao_entra(
        self, organization_a: Organization
    ) -> None:
        """Queda isolada em 24 h é o ruído normal de milhares de clientes."""
        from apps.dashboards.massivas import compute_mapa_do_dia

        set_current_organization(organization_a)
        self._cto(organization_a, "CTO-1")
        self._queda(organization_a, login="sozinho", cto="CTO-1")

        mapa = compute_mapa_do_dia(organization_a, now=timezone.now())
        assert mapa["pontos"] == []
        # Mas ela não some da contagem: o slide declara o que ficou de fora.
        assert mapa["caixas_com_queda"] == 1
        assert mapa["quedas"] == 1

    def test_queda_de_ontem_fica_fora_da_janela(
        self, organization_a: Organization
    ) -> None:
        from apps.dashboards.massivas import compute_mapa_do_dia

        set_current_organization(organization_a)
        self._cto(organization_a, "CTO-1")
        self._queda(organization_a, login="velha1", cto="CTO-1", horas_atras=30)
        self._queda(organization_a, login="velha2", cto="CTO-1", horas_atras=28)

        mapa = compute_mapa_do_dia(organization_a, now=timezone.now())
        assert mapa["quedas"] == 0
        assert mapa["pontos"] == []

    def test_queda_sem_caixa_e_contada_a_parte(
        self, organization_a: Organization
    ) -> None:
        """O mapa é menor que o dia, e a tela diz isso."""
        from apps.dashboards.massivas import compute_mapa_do_dia

        set_current_organization(organization_a)
        self._queda(organization_a, login="sem-cto", cto="")

        mapa = compute_mapa_do_dia(organization_a, now=timezone.now())
        assert mapa["sem_cto"] == 1
        assert mapa["pontos"] == []

    def test_caixa_sem_coordenada_nao_vira_ponto_e_e_declarada(
        self, organization_a: Organization
    ) -> None:
        from apps.dashboards.massivas import compute_mapa_do_dia

        set_current_organization(organization_a)
        # Sem elemento cadastrado: não há onde desenhar o ponto.
        self._queda(organization_a, login="b1", cto="CTO-SEM-MAPA")
        self._queda(organization_a, login="b2", cto="CTO-SEM-MAPA")

        mapa = compute_mapa_do_dia(organization_a, now=timezone.now())
        assert mapa["pontos"] == []
        assert mapa["caixas_sem_coordenada"] == 1

    def test_o_tamanho_do_circulo_cresce_com_as_quedas(self) -> None:
        """Raiz quadrada: a caixa de 2 quedas e a de 500 na mesma tela."""
        import json

        from apps.dashboards.charts import day_heat_map

        dados = {
            "pontos": [
                {"lat": -23.5, "lon": -47.4, "quedas": 100, "fora": 3, "label": "grande"},
                {"lat": -23.6, "lon": -47.5, "quedas": 2, "fora": 0, "label": "pequena"},
            ]
        }
        trace = json.loads(day_heat_map(dados))["data"][0]
        grande, pequena = trace["marker"]["size"]
        assert grande > pequena
        # A pequena continua visível a quatro metros — é o piso do tamanho.
        assert pequena >= 10
        # Vermelho onde ainda há gente fora, âmbar onde o dia passou.
        assert trace["marker"]["color"] == ["#ef4444", "#f59e0b"]

    def test_a_tv_diz_de_quem_e_e_que_tela_e(
        self, organization_a: Organization
    ) -> None:
        """Identidade no topo (pedido do operador).

        Uma TV sem título vira "aquele monitor": quem entra na sala não sabe se
        está vendo a rede, o financeiro ou um print esquecido de ontem.
        """
        token = _tv_pareada(organization_a)
        tv = Client()
        tv.cookies[COOKIE_NOME] = token
        html = tv.get(PANEL_URL).content.decode()
        assert "VELUS" in html
        assert "Quedas e massivas em tempo real" in html

    def test_slide_sai_da_rotacao_sem_caixa_no_mapa(self) -> None:
        """Mapa vazio na parede ensina a sala a ignorar a TV (T4)."""
        from apps.dashboards.panels.noc import _tem_mapa_do_dia

        assert _tem_mapa_do_dia({"mapa_dia": {"pontos": []}}) is False
        assert _tem_mapa_do_dia({"mapa_dia": {"pontos": [{"lat": 1}]}}) is True
