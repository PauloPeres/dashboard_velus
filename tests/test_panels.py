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
from apps.dashboards.panels.pairing import COOKIE_NOME, hash_segredo, novo_segredo
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

    def test_painel_sem_massiva_esconde_o_slide_de_mapa(
        self, organization_a: Organization
    ) -> None:
        """Slide sem pergunta treina a sala a ignorar a TV."""
        panel = get_panel("noc")
        assert panel is not None
        visiveis = [s.key for s in panel.visible_slides({"massivas": []})]
        assert "mapa" not in visiveis
        com_evento = [s.key for s in panel.visible_slides({"massivas": [{"id": 1}]})]
        assert "mapa" in com_evento

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
