"""Escalonamento de massiva crítica para o Telegram (P7 do painel de NOC).

A regra, em uma frase: **a TV é consciência contínua; o push exige ação de
alguém.** Estes testes travam os quatro filtros que impedem o canal de virar
barulho — porque canal de alerta que vira barulho é canal que alguém silencia, e
aí se perde o único aviso que alcança quem não está olhando para a tela.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.network.application.escalation import (
    escalar_massivas,
    massivas_para_escalar,
    mensagem_de,
)
from apps.network.infrastructure.models import OutageEvent
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


def _massiva(
    org: Organization,
    *,
    scope: str = OutageEvent.Scope.OLT,
    minutos: int = 30,
    afetados: int = 40,
    **extra: Any,
) -> OutageEvent:
    set_current_organization(org)
    agora = timezone.now()
    return OutageEvent.objects.create(
        organization=org,
        started_at=agora - timedelta(minutes=minutos),
        last_detected_at=agora,
        scope=scope,
        element_external_id="1",
        element_label="OLT 1",
        confidence=OutageEvent.Confidence.HIGH,
        affected_count=afetados,
        **extra,
    )


@pytest.mark.django_db
class TestQuemEscala:
    def test_critica_sem_dono_e_antiga_entra(self, organization_a: Organization) -> None:
        _massiva(organization_a)
        assert len(massivas_para_escalar(organization_a)) == 1

    def test_massiva_de_caixa_nao_escala(self, organization_a: Organization) -> None:
        """O que está na lista fica na lista: só infraestrutura vira push."""
        _massiva(organization_a, scope=OutageEvent.Scope.CTO)
        assert massivas_para_escalar(organization_a) == []

    def test_reconhecida_nao_escala(self, organization_a: Organization) -> None:
        """Reconhecimento é a mensagem "estou tratando" — insistir depois dela é
        transformar aviso em barulho."""
        _massiva(organization_a, acknowledged_at=timezone.now())
        assert massivas_para_escalar(organization_a) == []

    def test_recente_ainda_nao_escala(self, organization_a: Organization) -> None:
        """Primeiro a TV mostra; o celular toca se o evento sobreviver ao tempo
        em que alguém normalmente já teria pegado."""
        _massiva(organization_a, minutos=5)
        assert massivas_para_escalar(organization_a) == []

    def test_encerrada_nao_escala(self, organization_a: Organization) -> None:
        _massiva(organization_a, ended_at=timezone.now())
        assert massivas_para_escalar(organization_a) == []

    def test_manutencao_programada_nao_acorda_ninguem(
        self, organization_a: Organization
    ) -> None:
        """Acordar alguém pelo que foi avisado é o caminho mais curto para a
        equipe silenciar o canal."""
        _massiva(organization_a, maintenance_event_id=7, maintenance_label="troca de poste")
        assert massivas_para_escalar(organization_a) == []

    def test_ja_escalada_nao_repete(self, organization_a: Organization) -> None:
        """Sem isso, o loop de 5 min viraria uma mensagem a cada 5 min."""
        _massiva(organization_a, escalated_at=timezone.now())
        assert massivas_para_escalar(organization_a) == []


@pytest.mark.django_db
class TestEnvio:
    def test_desligado_nao_envia_e_nao_carimba(
        self, organization_a: Organization
    ) -> None:
        """Carimbar sem enviar registraria um aviso que ninguém recebeu — e a
        massiva perderia a chance de escalar quando o canal voltasse."""
        outage = _massiva(organization_a)
        with override_settings(TELEGRAM_ENABLED=False):
            resultado = escalar_massivas(organization_a)
        assert resultado == {"candidatas": 1, "enviadas": 0}
        outage.refresh_from_db()
        assert outage.escalated_at is None

    def test_envio_bem_sucedido_carimba(
        self, organization_a: Organization, monkeypatch: Any
    ) -> None:
        outage = _massiva(organization_a)
        enviadas: list[str] = []
        monkeypatch.setattr(
            "apps.shared.notifications.telegram.enviar_telegram",
            lambda texto, **_: (enviadas.append(texto), True)[1],
        )
        resultado = escalar_massivas(organization_a)
        assert resultado["enviadas"] == 1
        outage.refresh_from_db()
        assert outage.escalated_at is not None
        assert "Massiva sem ninguém tratando" in enviadas[0]

    def test_falha_no_envio_deixa_a_massiva_elegivel(
        self, organization_a: Organization, monkeypatch: Any
    ) -> None:
        outage = _massiva(organization_a)
        monkeypatch.setattr(
            "apps.shared.notifications.telegram.enviar_telegram", lambda *a, **k: False
        )
        escalar_massivas(organization_a)
        outage.refresh_from_db()
        assert outage.escalated_at is None
        assert len(massivas_para_escalar(organization_a)) == 1


@pytest.mark.django_db
class TestMensagem:
    def test_diz_o_que_onde_quanto_e_ha_quanto_tempo(
        self, organization_a: Organization
    ) -> None:
        outage = _massiva(organization_a, afetados=40, minutos=42)
        texto = mensagem_de(outage)
        assert "OLT 1" in texto
        assert "40" in texto
        assert "42 min" in texto

    def test_nao_promete_causa(self, organization_a: Organization) -> None:
        """O veredito automático pode estar errado, e no push não há espaço para
        a ressalva que a tela sempre carrega."""
        outage = _massiva(organization_a)
        texto = mensagem_de(outage).lower()
        assert "energia" not in texto
        assert "fibra" not in texto
        assert "rompimento" not in texto
