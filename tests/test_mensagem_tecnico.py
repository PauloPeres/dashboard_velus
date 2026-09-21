"""A mensagem pronta para o técnico (pedido de 21/09/2026).

O operador pediu um botão que copia o que o técnico precisa para sair de casa
sabendo para onde ir: quantos estão fora, onde é, o ponto no mapa, a caixa e o
cabo que passa ali.

O que estes testes protegem é o que a mensagem **não** pode fazer: dar certeza
onde há inferência. Mandar um técnico com falsa certeza é pior que mandá-lo sem
informação — ele para de procurar onde deveria.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.dashboards.massivas import compute_mensagem_tecnico, outage_row
from apps.network.infrastructure.models import (
    ConnectionDropEvent,
    NetworkElement,
    OutageEvent,
)
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


def _massiva(org: Organization, **extra: Any) -> OutageEvent:
    set_current_organization(org)
    agora = timezone.now()
    campos = {
        "scope": OutageEvent.Scope.PON,
        "element_external_id": "364",
        "element_label": "PON 364",
        "suspected_segment_label": "trecho CX-01 → CX-02",
        "confidence": OutageEvent.Confidence.HIGH,
        "affected_count": 31,
        "restored_count": 4,
        "affected_fraction": 0.46,
        "mrr_at_risk": Decimal("2800.00"),
    }
    campos.update(extra)
    return OutageEvent.objects.create(
        organization=org,
        started_at=agora - timedelta(minutes=42),
        last_detected_at=agora,
        **campos,
    )


def _caixa(org: Organization, ext: str, nome: str, lat: float | None, lon: float | None) -> None:
    NetworkElement.objects.create(
        organization=org, source_type="IXC", kind=NetworkElement.Kind.CTO,
        external_id=ext, name=nome, latitude=lat, longitude=lon,
    )


def _queda(org: Organization, *, cto: str) -> ConnectionDropEvent:
    from apps.network.infrastructure.models import Connection

    conexao = Connection.objects.create(
        organization=org, source_type="IXC", external_id=f"conn-{cto}",
        customer_external_id=f"cust-{cto}", login=f"l-{cto}",
        status=Connection.Status.OFFLINE,
    )
    return ConnectionDropEvent.objects.create(
        organization=org, connection=conexao, login=f"l-{cto}",
        dropped_at=timezone.now(), cto_external_id=cto,
    )


@pytest.mark.django_db
class TestMensagem:
    def test_comeca_pelo_tamanho_do_problema(
        self, organization_a: Organization
    ) -> None:
        """É a primeira pergunta de quem recebe: quantos estão fora?"""
        outage = _massiva(organization_a)
        texto = compute_mensagem_tecnico(organization_a, outage_row(outage), [])
        assert texto.startswith("*MASSIVA* · 27 clientes fora agora")
        assert "31 afetados desde" in texto
        assert "(42 min)" in texto

    def test_leva_o_ponto_no_mapa_da_caixa(
        self, organization_a: Organization
    ) -> None:
        """O link é de BUSCA por coordenada, não de rota: quem recebe decide de
        onde sai, e uma rota pré-montada seria palpite sobre isso."""
        set_current_organization(organization_a)
        _caixa(organization_a, "CTO-1", "A34 - SP 13", -23.6048, -47.4883)
        outage = _massiva(organization_a)
        texto = compute_mensagem_tecnico(
            organization_a, outage_row(outage), [_queda(organization_a, cto="CTO-1")]
        )
        assert "https://www.google.com/maps/search/?api=1&query=-23.604800,-47.488300" in texto
        assert "A34 - SP 13" in texto

    def test_sem_caixa_com_coordenada_nao_inventa_ponto(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        _caixa(organization_a, "CTO-1", "sem geo", None, None)
        outage = _massiva(organization_a)
        texto = compute_mensagem_tecnico(
            organization_a, outage_row(outage), [_queda(organization_a, cto="CTO-1")]
        )
        assert "google.com/maps" not in texto
        assert "sem geo" in texto  # a caixa ainda é nomeada

    def test_trecho_vai_com_a_ressalva_de_que_e_inferencia(
        self, organization_a: Organization
    ) -> None:
        """Mandar o técnico com falsa certeza é pior que mandá-lo sem
        informação: ele para de procurar onde deveria."""
        outage = _massiva(organization_a)
        texto = compute_mensagem_tecnico(organization_a, outage_row(outage), [])
        assert "trecho CX-01 → CX-02" in texto
        assert "inferência pelo cadastro" in texto

    def test_cabo_vai_como_candidato_com_a_distancia(
        self, organization_a: Organization
    ) -> None:
        outage = _massiva(organization_a)
        linha = outage_row(outage, cabos={
            "determinavel": True,
            "cabos": [{
                "external_id": "C1", "nome": "FIBRA AS80 12FO BACKBONE 18",
                "classe": "BACKBONE", "distancia_m": 0.0, "ctos_tocadas": 5,
            }],
        })
        texto = compute_mensagem_tecnico(organization_a, linha, [])
        assert "Cabo candidato: FIBRA AS80 12FO BACKBONE 18 (backbone)" in texto
        assert "5 caixas do evento" in texto

    def test_manutencao_programada_avisa_logo(
        self, organization_a: Organization
    ) -> None:
        """Quem recebe precisa saber antes de sair de casa que o evento era
        esperado."""
        outage = _massiva(
            organization_a, maintenance_event_id=7, maintenance_label="troca de poste"
        )
        texto = compute_mensagem_tecnico(organization_a, outage_row(outage), [])
        assert "manutenção programada" in texto

    def test_causa_provavel_entra_quando_o_veredito_opinou(
        self, organization_a: Organization
    ) -> None:
        outage = _massiva(organization_a)
        linha = outage_row(outage, veredito={
            "veredito": "fibra", "rotulo": "provável fibra",
            "base": "91% LOS em 31 quedas",
        })
        texto = compute_mensagem_tecnico(organization_a, linha, [])
        assert "Causa provável: provável fibra" in texto
        assert "91% LOS" in texto


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestBotaoNaTela:
    def test_card_traz_o_botao_de_copiar(
        self, client: Any, user_a: Any, organization_a: Organization
    ) -> None:
        _massiva(organization_a)
        client.force_login(user_a)
        html = client.get("/operations/massivas/").content.decode()
        assert "Copiar mensagem para o técnico" in html
        assert "data-mensagem=" in html
