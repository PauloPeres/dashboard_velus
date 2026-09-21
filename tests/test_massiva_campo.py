"""O link de campo — a massiva no mapa, aberta por quem não tem login (T6).

Decidido com o operador em 21/09/2026: **link assinado, válido por 24 horas,
escopado a uma massiva**. O técnico na rua precisa ver por onde o cabo corre e
onde estão as caixas; criar conta para cada pessoa que entra e sai seria trocar
um problema por outro.

É uma porta aberta na frente do sistema, então o que estes testes protegem é o
tamanho dela: link forjado não abre, link velho não abre, e o que a tela mostra
não inclui nome, documento nem telefone de cliente — quem está na rua precisa
saber onde cavar, não quem mora ali.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core import signing
from django.test import Client
from django.utils import timezone

from apps.customers.infrastructure.models import Customer
from apps.dashboards.campo import VALIDADE_SEGUNDOS, assinar, ler
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    NetworkElement,
    OutageAffectedLogin,
    OutageEvent,
)
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


def _massiva(org: Organization) -> OutageEvent:
    set_current_organization(org)
    agora = timezone.now()
    return OutageEvent.objects.create(
        organization=org,
        started_at=agora - timedelta(minutes=30),
        last_detected_at=agora,
        scope=OutageEvent.Scope.PON,
        element_external_id="364",
        element_label="PON 364",
        suspected_segment_label="trecho CX-01 → CX-02",
        confidence=OutageEvent.Confidence.HIGH,
        affected_count=12,
        restored_count=3,
    )


class TestToken:
    def test_ida_e_volta(self) -> None:
        dados = ler(assinar(42, 7))
        assert dados == {"o": 42, "org": 7}

    def test_token_adulterado_nao_abre(self) -> None:
        """Trocar o id da massiva na URL invalida a assinatura — é o ponto de
        assinar em vez de só esconder o id."""
        token = assinar(42, 7)
        assert ler(token[:-3] + "xyz") is None

    def test_token_velho_nao_abre(self, monkeypatch: Any) -> None:
        """Link esquecido num grupo de WhatsApp não pode servir na semana que
        vem — 24 h cobrem o turno e a virada de plantão, e nada além disso."""
        token = assinar(42, 7)
        assert ler(token) is not None

        # O relógio do `signing` é o do `time`; adiantá-lo em 25 h é a forma
        # honesta de testar a expiração sem esperar um dia.
        real = signing.time.time
        monkeypatch.setattr(
            signing.time, "time", lambda: real() + VALIDADE_SEGUNDOS + 3600
        )
        assert ler(token) is None

    def test_lixo_nao_abre(self) -> None:
        assert ler("") is None
        assert ler("nao-e-token") is None


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestTelaDeCampo:
    def test_abre_sem_login(self, organization_a: Organization) -> None:
        outage = _massiva(organization_a)
        token = assinar(outage.pk, organization_a.pk)
        resp = Client().get(f"/campo/massiva/{token}/")
        assert resp.status_code == 200
        html = resp.content.decode()
        assert "trecho CX-01 → CX-02" in html
        assert "vale 24 h" in html or "válido por 24 h" in html

    def test_nao_mostra_dado_pessoal(self, organization_a: Organization) -> None:
        """O link circula por WhatsApp. Quem está na rua precisa saber onde
        cavar, não quem mora ali."""
        outage = _massiva(organization_a)
        set_current_organization(organization_a)
        cliente = Customer.objects.create(
            organization=organization_a, source_type="IXC", external_id="c1",
            name="FULANO DE TAL", phone="(15) 99128-2181", document="12345678901",
        )
        conexao = Connection.objects.create(
            organization=organization_a, source_type="IXC", external_id="conn-1",
            customer_external_id="c1", login="fulano123",
            status=Connection.Status.OFFLINE,
        )
        queda = ConnectionDropEvent.objects.create(
            organization=organization_a, connection=conexao, customer=cliente,
            login="fulano123", dropped_at=timezone.now(), cto_external_id="CTO-1",
            latitude=-23.5, longitude=-47.4,
        )
        OutageAffectedLogin.objects.create(
            organization=organization_a, outage=outage, drop_event=queda,
            login=queda.login, dropped_at=queda.dropped_at,
        )

        html = Client().get(f"/campo/massiva/{assinar(outage.pk, organization_a.pk)}/").content.decode()
        assert "FULANO DE TAL" not in html
        assert "12345678901" not in html
        assert "99128-2181" not in html

    def test_token_de_outra_massiva_nao_abre_esta(
        self, organization_a: Organization
    ) -> None:
        _massiva(organization_a)
        resp = Client().get("/campo/massiva/token-invalido/")
        assert resp.status_code == 404
        assert "Link expirado" in resp.content.decode()

    def test_massiva_de_outra_org_nao_abre(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        """A organização entra na assinatura: sem ela, um id válido noutra base
        abriria a massiva errada."""
        alheia = _massiva(organization_b)
        token = assinar(alheia.pk, organization_a.pk)
        assert Client().get(f"/campo/massiva/{token}/").status_code == 404

    def test_mapa_e_o_mesmo_da_aba(self, organization_a: Organization) -> None:
        """Um segundo desenho com regras próprias faria as duas telas
        discordarem sobre onde está o problema."""
        outage = _massiva(organization_a)
        set_current_organization(organization_a)
        NetworkElement.objects.create(
            organization=organization_a, source_type="IXC",
            kind=NetworkElement.Kind.CTO, external_id="CTO-1", name="A34 - SP 13",
            latitude=-23.6, longitude=-47.48,
        )
        html = Client().get(f"/campo/massiva/{assinar(outage.pk, organization_a.pk)}/").content.decode()
        assert "mapa-campo-data" in html
        assert "não</strong> é onde a fibra está enterrada hoje" in html


@pytest.mark.django_db
class TestMensagemComLink:
    def test_mensagem_leva_os_dois_links(self, organization_a: Organization) -> None:
        """São perguntas diferentes: o mapa da massiva é o que se olha ao
        chegar; o do Google Maps é o que abre o GPS do carro."""
        from apps.dashboards.massivas import compute_mensagem_tecnico, outage_row

        outage = _massiva(organization_a)
        texto = compute_mensagem_tecnico(
            organization_a, outage_row(outage), [], base_url="https://velus.exemplo.com"
        )
        assert "https://velus.exemplo.com/campo/massiva/" in texto
        assert "vale 24h" in texto

    def test_sem_endereco_publico_nao_monta_link_quebrado(
        self, organization_a: Organization
    ) -> None:
        from apps.dashboards.massivas import compute_mensagem_tecnico, outage_row

        outage = _massiva(organization_a)
        texto = compute_mensagem_tecnico(organization_a, outage_row(outage), [])
        assert "/campo/massiva/" not in texto
