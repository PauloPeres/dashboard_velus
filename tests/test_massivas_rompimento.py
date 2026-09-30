"""Onde rompeu, a evidência de "no ar" e o padrão por PON (massiva 359, 30/09/2026).

Três coisas que a massiva 359 ensinou, e que estes testes travam:

1. caixa sem cliente online não é evidência de fibra boa — contá-la jogou o
   "comece por aqui" para o POP, a 1.155 m do rompimento;
2. o local do rompimento é o gabarito: gravado, ele mede cada resposta da tela;
   colado errado (sem coordenada, ou a 20 km da massiva), não grava nada;
3. o padrão por PON diz o tipo do problema antes do mapa — PON inteira, corte
   de tronco — e fica calado quando não sabe.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.dashboards.massivas import ctos_com_cliente_no_ar
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    NetworkElement,
    NetworkElementGeometry,
    OutageAffectedLogin,
    OutageEvent,
)
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User

URL = "/operations/massivas/"


# =============================================================================
# Helpers de seed
# =============================================================================


def _conexao(
    org: Organization,
    *,
    login: str,
    status: str = Connection.Status.OFFLINE,
    cto: str = "",
    pon: str = "",
    olt: str = "1",
) -> Connection:
    set_current_organization(org)
    return Connection.objects.create(
        organization=org,
        source_type="IXC",
        external_id=f"conn-{org.slug}-{login}",
        customer_external_id=f"cust-{login}",
        login=login,
        status=status,
        cto_external_id=cto,
        pon_external_id=pon,
        transmitter_external_id=olt,
    )


def _queda(
    org: Organization,
    *,
    login: str,
    cto: str = "",
    lat: float | None = None,
    lon: float | None = None,
    pon: str = "",
    olt: str = "1",
) -> ConnectionDropEvent:
    conexao = _conexao(org, login=login, cto=cto, pon=pon, olt=olt)
    return ConnectionDropEvent.objects.create(
        organization=org,
        connection=conexao,
        login=login,
        dropped_at=timezone.now() - timedelta(minutes=20),
        cto_external_id=cto,
        pon_external_id=pon,
        transmitter_external_id=olt,
        latitude=lat,
        longitude=lon,
        monthly_amount=Decimal("99.90"),
    )


def _massiva(org: Organization, quedas: list[ConnectionDropEvent]) -> OutageEvent:
    set_current_organization(org)
    now = timezone.now()
    outage = OutageEvent.objects.create(
        organization=org,
        started_at=now - timedelta(minutes=30),
        last_detected_at=now,
        scope=OutageEvent.Scope.OLT,
        element_external_id="1",
        element_label="OLT 1",
        confidence=OutageEvent.Confidence.MEDIUM,
        affected_count=len(quedas),
        affected_fraction=0.3,
        mrr_at_risk=Decimal("99.90"),
    )
    for queda in quedas:
        OutageAffectedLogin.objects.create(
            organization=org,
            outage=outage,
            drop_event=queda,
            login=queda.login,
            dropped_at=queda.dropped_at,
        )
    return outage


def _planta(org: Organization) -> None:
    """POP → CEO → CTO-1 → CTO-2 (vazia) num cabo; um ramo da CEO até a CTO-3.

    POP
     |
    CEO ── CTO-3 (com cliente online)
     |
    CTO-1
     |
    CTO-2 (sem cliente)
    """
    set_current_organization(org)
    NetworkElement.objects.create(
        organization=org,
        source_type="IXC",
        kind=NetworkElement.Kind.POP,
        external_id="POP-1",
        name="POP Centro",
        latitude=-23.5000,
        longitude=-47.45,
    )
    for ident, lat, lon in (
        ("CTO-1", -23.5036, -47.45),
        ("CTO-2", -23.5054, -47.45),
        ("CTO-3", -23.5018, -47.4480),
    ):
        NetworkElement.objects.create(
            organization=org,
            source_type="IXC",
            kind=NetworkElement.Kind.CTO,
            external_id=ident,
            name=ident,
            latitude=lat,
            longitude=lon,
        )
    NetworkElementGeometry.objects.create(
        organization=org,
        source_type="IXC",
        kind=NetworkElement.Kind.SPLICE,
        external_id="CEO-1",
        name="Caixa de Emenda 1",
        points=[[-23.5018, -47.45]],
    )
    NetworkElementGeometry.objects.create(
        organization=org,
        source_type="IXC",
        kind=NetworkElement.Kind.CABLE,
        external_id="CB-1",
        name="FIBRA AS80 72FO 1",
        type_name="FIBRA AS80 72FO",
        points=[[-23.5000, -47.45], [-23.5018, -47.45], [-23.5036, -47.45], [-23.5054, -47.45]],
    )
    NetworkElementGeometry.objects.create(
        organization=org,
        source_type="IXC",
        kind=NetworkElement.Kind.CABLE,
        external_id="CB-2",
        name="FIBRA AS80 06 FO ATENDIMENTO 1",
        type_name="FIBRA AS80 06 FO ATENDIMENTO",
        # Só as duas pontas: um vértice solto no meio ficaria a menos de 300 m
        # do POP, e o grafo o ligaria ao POP (a tolerância do prédio do POP) —
        # a CEO passaria a pender do POP pelo ramo, não pelo tronco.
        points=[[-23.5018, -47.45], [-23.5018, -47.4480]],
    )


def _massiva_na_planta(org: Organization) -> OutageEvent:
    """A CTO-1 cai; a CTO-3 tem cliente online; a CTO-2 não tem cliente nenhum."""
    _planta(org)
    _conexao(org, login="vizinho", status=Connection.Status.ONLINE, cto="CTO-3")
    return _massiva(org, [_queda(org, login="a1", cto="CTO-1", lat=-23.5036, lon=-47.45)])


# =============================================================================
# 1. "No ar" é caixa com cliente online
# =============================================================================


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestNoArEhClienteOnline:
    def test_caixa_vazia_abaixo_nao_impede_o_comece_por_aqui(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """A CTO-2, abaixo da CTO-1, não tem cliente. Antes ela contava como
        "no ar" e a CTO-1 nunca se confirmava como ponto de partida."""
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        rota = resp.context["rota_tecnico"]
        assert rota["partida"]["id"] == "CTO-1"
        assert rota["partida_confirmada"] is True
        assert "COMECE POR" in resp.context["outage"]["mensagem_tecnico"]

    def test_cliente_da_propria_massiva_nao_conta_como_no_ar(
        self, organization_a: Organization
    ) -> None:
        """Depois do reparo o cliente volta a ficar online — e a caixa dele
        seria "fora" e "no ar" ao mesmo tempo."""
        queda = _queda(organization_a, login="a1", cto="CTO-1")
        Connection.objects.filter(pk=queda.connection_id).update(status=Connection.Status.ONLINE)
        _conexao(organization_a, login="vizinho", status=Connection.Status.ONLINE, cto="CTO-3")
        assert ctos_com_cliente_no_ar(organization_a, [queda]) == {"CTO-3"}

    def test_as_hipoteses_chegam_na_tela_e_no_mapa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        hipoteses = resp.context["rota_tecnico"]["hipoteses"]
        assert hipoteses[0]["no"]["id"] == "CTO-1"
        assert hipoteses[0]["cabo"] == "FIBRA AS80 72FO 1"
        assert "Hipóteses de rompimento" in resp.content.decode()
        # A 1ª hipótese vira linha sobre o cabo, e o cabo dela entra no desenho
        # como candidato — na 359 ele só aparecia como linha cinza de contexto.
        mapa = resp.context["mapa"]
        assert mapa["hipoteses"]
        assert mapa["hipoteses"][0]["pontos"]
        assert any("72FO 1" in c["nome"] for c in mapa["cabos"])


# =============================================================================
# 2. Onde rompeu — o gabarito
# =============================================================================


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestOndeRompeu:
    def test_link_do_maps_grava_o_ponto_e_a_afericao(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """O ponto chega como chegou na 359: link do Google Maps colado."""
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"{URL}{outage.pk}/rompimento/",
            {"local": "https://www.google.com/maps?q=-23.503000,-47.450000"},
        )
        assert resp.status_code == 302
        assert resp["Location"].endswith(f"{URL}{outage.pk}/#rompimento")

        outage.refresh_from_db()
        assert (outage.break_latitude, outage.break_longitude) == (-23.503, -47.45)
        assert outage.break_recorded_by == user_a
        afericao = outage.break_evaluation
        assert afericao["algoritmo"] == "verossimilhanca-v1"
        # O ponto está sobre o trecho CEO → CTO-1: a 1ª hipótese acertou, e a
        # caixa "comece por aqui" (CTO-1) está a ~67 m.
        assert afericao["erros_m"]["hipotese_1"] == 0
        assert afericao["acertou"] is True
        assert 60 <= afericao["erros_m"]["partida"] <= 75
        assert afericao["posicao_do_trecho_certo"] == 1
        # A evidência vai junto: é ela que o backtest refaz com a regra nova.
        assert afericao["evidencia"] == {"fora": ["CTO-1"], "no_ar": ["CTO-3"]}

        resp = client.get(f"{URL}{outage.pk}/")
        html = resp.content.decode()
        assert "Onde rompeu" in html
        assert "acertou" in html
        assert resp.context["mapa"]["rompimento_registrado"]

    def test_texto_sem_coordenada_nao_grava(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """O link encurtado não traz a coordenada — e resolvê-lo seria chamar o Google."""
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"{URL}{outage.pk}/rompimento/", {"local": "https://maps.app.goo.gl/abc123"}
        )
        assert "rompimento=invalido" in resp["Location"]
        outage.refresh_from_db()
        assert outage.break_latitude is None

    def test_latitude_e_longitude_trocadas_nao_gravam(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        resp = client.post(f"{URL}{outage.pk}/rompimento/", {"local": "-47.45, -23.503"})
        assert "rompimento=longe" in resp["Location"]
        outage.refresh_from_db()
        assert outage.break_latitude is None

    def test_massiva_de_outra_organizacao_nao_existe(
        self, client: Any, user_a: User, organization_b: Organization
    ) -> None:
        outage = _massiva(organization_b, [_queda(organization_b, login="b1", cto="X")])
        client.force_login(user_a)
        resp = client.post(f"{URL}{outage.pk}/rompimento/", {"local": "-23.5, -47.45"})
        assert resp.status_code == 404
        set_current_organization(organization_b)
        outage.refresh_from_db()
        assert outage.break_latitude is None

    def test_a_fila_de_causa_aceita_o_local_junto(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        client.post(
            f"{URL}{outage.pk}/causa/",
            {"confirmed_cause": "ROMPIMENTO", "local": "-23.503, -47.45"},
        )
        outage.refresh_from_db()
        assert outage.confirmed_cause == "ROMPIMENTO"
        assert outage.break_latitude == -23.503

    def test_local_invalido_na_fila_nao_grava_nem_a_causa(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Gravar a causa e perder o ponto tiraria a massiva da fila sem ele."""
        outage = _massiva_na_planta(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"{URL}{outage.pk}/causa/",
            {"confirmed_cause": "ROMPIMENTO", "local": "perto da padaria"},
        )
        assert "erro=local" in resp["Location"]
        outage.refresh_from_db()
        assert outage.confirmed_cause == ""
        assert outage.break_latitude is None


# =============================================================================
# 3. O padrão por PON
# =============================================================================


def _pons(org: Organization, *, fora: dict[str, int], no_ar: dict[str, int]) -> OutageEvent:
    quedas = [
        _queda(org, login=f"f-{pon}-{i}", cto=f"CTO-{pon}", pon=pon)
        for pon, n in fora.items()
        for i in range(n)
    ]
    for pon, n in no_ar.items():
        for i in range(n):
            _conexao(
                org,
                login=f"u-{pon}-{i}",
                status=Connection.Status.ONLINE,
                cto=f"CTO-{pon}",
                pon=pon,
            )
    return _massiva(org, quedas)


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPadraoPorPon:
    def test_pon_inteira_vira_rotulo_na_tela_e_na_mensagem(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _pons(organization_a, fora={"10": 3}, no_ar={"11": 3, "12": 3})
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        assert resp.context["outage"]["padrao_pon"]["tipo"] == "PON"
        assert "PON inteira fora (PON 10)" in resp.content.decode()
        assert "PON inteira fora (PON 10)" in resp.context["outage"]["mensagem_tecnico"]

    def test_varias_pons_inteiras_com_as_vizinhas_no_ar_e_corte_de_tronco(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _pons(organization_a, fora={"10": 3, "11": 4}, no_ar={"12": 3, "13": 5})
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        assert "Corte de tronco: 2 PONs inteiras fora" in resp.content.decode()

    def test_queda_parcial_fica_sem_rotulo(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """Um de três fora é ramal, energia, ONU — não se sabe, não se rotula."""
        outage = _pons(organization_a, fora={"10": 1}, no_ar={"10": 2, "11": 3})
        client.force_login(user_a)
        resp = client.get(f"{URL}{outage.pk}/")
        assert resp.context["outage"]["padrao_pon"]["tipo"] == ""
        assert "Padrão por PON" not in resp.content.decode()

    def test_o_padrao_aparece_na_lista_de_abertas(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _pons(organization_a, fora={"10": 3, "11": 4}, no_ar={"12": 3, "13": 5})
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Corte de tronco: 2 PONs inteiras fora" in html
