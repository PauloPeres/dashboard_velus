"""Causa confirmada (R9) e manutenção programada (R10).

Esta é a **primeira entrada de dados** do dashboard, que até aqui só lia do IXC e
do warehouse. A troca foi decidida com o operador em 19/09/2026, com um objetivo
declarado: o veredito automático (energia vs. fibra) adivinha e nunca fica
sabendo se acertou, e sem alguém registrando o que a massiva era de verdade nada
se calibra — nem o veredito, nem, depois, um modelo treinado no passado.

O que estes testes protegem:

1. **o vocabulário é fechado.** Causa fora da lista não vira "outro" nem vazio
   silencioso: o campo é rótulo de treino, e um valor inventado contamina a
   série inteira;
2. **causa confirmada não apaga o palpite.** É a divergência entre os dois que
   mede o acerto — guardar só a resposta destruiria a aferição;
3. **manutenção só marca no nascimento, e só no próprio escopo.** Janela
   cadastrada depois não reescreve o passado, e janela de OLT não marca como
   esperada uma caixa que nada tem a ver com o aviso;
4. **quem preencheu fica registrado.** Rótulo sem autor não se audita.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.atendimento.application.manutencao import janela_que_cobre
from apps.atendimento.infrastructure.models import EventoRede
from apps.dashboards.massivas import (
    compute_placar_do_veredito,
    compute_sem_causa,
    normalize_tags,
    outage_row,
)
from apps.network.infrastructure.models import OutageEvent
from apps.shared.context import set_current_organization
from apps.tenancy.models import AccessGroup, Organization, OrganizationMembership, User

URL = "/operations/massivas/"


def _outage(
    org: Organization,
    *,
    ended: bool = True,
    causa: str = "",
    scope: str = OutageEvent.Scope.OLT,
    element_id: str = "1",
    inicio_min_atras: int = 120,
) -> OutageEvent:
    set_current_organization(org)
    now = timezone.now()
    return OutageEvent.objects.create(
        organization=org,
        started_at=now - timedelta(minutes=inicio_min_atras),
        ended_at=now - timedelta(minutes=inicio_min_atras - 60) if ended else None,
        last_detected_at=now,
        scope=scope,
        element_external_id=element_id,
        element_label=f"OLT {element_id}",
        confidence=OutageEvent.Confidence.MEDIUM,
        affected_count=9,
        restored_count=9,
        affected_fraction=0.4,
        confirmed_cause=causa,
        mrr_at_risk=Decimal("100.00"),
    )


def _janela(
    org: Organization,
    *,
    scope: str = "",
    element_id: str = "",
    inicio_min_atras: int = 180,
    duracao_min: int = 240,
    pontual: bool = False,
) -> EventoRede:
    set_current_organization(org)
    inicio = timezone.now() - timedelta(minutes=inicio_min_atras)
    return EventoRede.objects.create(
        organization=org,
        tipo=EventoRede.Tipo.MANUTENCAO,
        titulo="Troca de cordoalha",
        started_at=inicio,
        ended_at=None if pontual else inicio + timedelta(minutes=duracao_min),
        scope=scope,
        element_external_id=element_id,
    )


# =============================================================================
# O vocabulário fechado
# =============================================================================
class TestVocabulario:
    def test_tag_fora_da_lista_e_descartada(self) -> None:
        """Texto livre viraria sinônimo ("vandalismo", "vandalizado", "roubo de
        cabo") que nenhum modelo junta depois."""
        assert normalize_tags(["vandalismo", "gato_comeu"]) == ["vandalismo"]

    def test_ordem_das_tags_e_a_do_vocabulario(self) -> None:
        """Saída estável não é estética: é o que permite comparar duas massivas
        sem normalizar de novo."""
        assert normalize_tags(["chuva", "vandalismo"]) == ["vandalismo", "chuva"]


# =============================================================================
# A fila e a gravação
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestFilaDeCausa:
    def test_encerrada_sem_causa_entra_na_fila(
        self, organization_a: Organization
    ) -> None:
        _outage(organization_a)
        fila = compute_sem_causa(organization_a)
        assert fila["total"] == 1

    def test_aberta_nao_entra_na_fila(self, organization_a: Organization) -> None:
        """Perguntar a causa no meio do reparo é atrapalhar quem está atendendo —
        e a resposta ainda não existe."""
        _outage(organization_a, ended=False)
        assert compute_sem_causa(organization_a)["total"] == 0

    def test_com_causa_sai_da_fila(self, organization_a: Organization) -> None:
        _outage(organization_a, causa=OutageEvent.Cause.ROMPIMENTO)
        assert compute_sem_causa(organization_a)["total"] == 0

    def test_post_grava_causa_tags_e_autor(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"/operations/massivas/{outage.pk}/causa/",
            {
                "confirmed_cause": OutageEvent.Cause.ROMPIMENTO.value,
                "cause_tags": ["vandalismo", "troca_de_poste"],
                "cause_note": "poste derrubado na curva",
            },
        )
        assert resp.status_code == 302
        outage.refresh_from_db()
        assert outage.confirmed_cause == OutageEvent.Cause.ROMPIMENTO.value
        assert outage.cause_tags == ["vandalismo", "troca_de_poste"]
        assert outage.cause_note == "poste derrubado na curva"
        assert outage.cause_confirmed_by_id == user_a.pk
        assert outage.cause_confirmed_at is not None

    def test_causa_fora_do_vocabulario_nao_grava(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        """O campo é rótulo de treino: valor inventado contamina a série, então
        o certo é recusar e devolver para a tela — não gravar um "outro"."""
        outage = _outage(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"/operations/massivas/{outage.pk}/causa/",
            {"confirmed_cause": "METEORO"},
        )
        assert resp.status_code == 302
        assert "erro=1" in resp["Location"]
        outage.refresh_from_db()
        assert outage.confirmed_cause == ""

    def test_massiva_de_outra_org_responde_404(
        self, client: Any, user_a: User, organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        alheia = _outage(organization_b)
        set_current_organization(organization_a)
        client.force_login(user_a)
        resp = client.post(
            f"/operations/massivas/{alheia.pk}/causa/",
            {"confirmed_cause": OutageEvent.Cause.ENERGIA.value},
        )
        assert resp.status_code == 404
        alheia.refresh_from_db()
        assert alheia.confirmed_cause == ""

    def test_sem_acesso_a_aba_nao_grava(
        self, client: Any, organization_a: Organization
    ) -> None:
        """A permissão é a da aba — quem não a enxerga não escreve nela."""
        outage = _outage(organization_a)
        grupo = AccessGroup.objects.create(
            organization=organization_a, name="Só financeiro", allowed_pages=["revenue"]
        )
        user = User.objects.create_user(email="sem@acesso.com")
        OrganizationMembership.objects.create(
            user=user, organization=organization_a,
            role=OrganizationMembership.Role.MEMBER, is_active=True, access_group=grupo,
        )
        client.force_login(user)
        client.post(
            f"/operations/massivas/{outage.pk}/causa/",
            {"confirmed_cause": OutageEvent.Cause.ENERGIA.value},
        )
        outage.refresh_from_db()
        assert outage.confirmed_cause == ""

    def test_a_tela_mostra_a_fila_e_o_motivo_de_pedir(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        _outage(organization_a)
        client.force_login(user_a)
        html = client.get(URL).content.decode()
        assert "Massivas esperando causa" in html
        assert "nunca fica sabendo se acertou" in html


# =============================================================================
# A causa não apaga o palpite
# =============================================================================
@pytest.mark.django_db
class TestCausaConviveComOVeredito:
    def test_linha_traz_causa_confirmada_e_segue_trazendo_o_veredito(
        self, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a, causa=OutageEvent.Cause.ENERGIA)
        veredito = {"veredito": "fibra", "rotulo": "provável fibra"}
        linha = outage_row(outage, veredito=veredito)
        assert linha["confirmed_cause"] == OutageEvent.Cause.ENERGIA.value
        assert linha["confirmed_cause_label"] == "Falta de energia"
        # O palpite errado continua ali: é ele que vira medida de acerto.
        assert linha["veredito"]["veredito"] == "fibra"

    def test_placar_sem_nenhuma_causa_confirmada_nao_inventa_numero(
        self, organization_a: Organization
    ) -> None:
        _outage(organization_a)
        placar = compute_placar_do_veredito(organization_a)
        assert placar["tem_amostra"] is False
        assert placar["total"] == 0

    def test_placar_ignora_causa_que_o_veredito_nao_tenta_adivinhar(
        self, organization_a: Organization
    ) -> None:
        """Manutenção e falso positivo não são energia nem fibra: contá-los como
        erro puniria o palpite por uma pergunta que ele não faz."""
        _outage(organization_a, causa=OutageEvent.Cause.MANUTENCAO)
        placar = compute_placar_do_veredito(organization_a)
        assert placar["total"] == 1
        assert placar["tem_amostra"] is False


# =============================================================================
# Manutenção programada
# =============================================================================
@pytest.mark.django_db
class TestJanelaDeManutencao:
    def test_janela_sem_escopo_cobre_tudo(self, organization_a: Organization) -> None:
        _janela(organization_a)
        janela = janela_que_cobre(
            organization_a, scope="OLT", element_external_id="1",
            moment=timezone.now() - timedelta(minutes=60),
        )
        assert janela is not None

    def test_janela_de_outro_elemento_nao_cobre(
        self, organization_a: Organization
    ) -> None:
        """Manutenção na OLT 2 não explica queda na OLT 1."""
        _janela(organization_a, scope="OLT", element_id="2")
        assert janela_que_cobre(
            organization_a, scope="OLT", element_external_id="1",
            moment=timezone.now() - timedelta(minutes=60),
        ) is None

    def test_janela_de_olt_nao_cobre_cto_abaixo_dela(
        self, organization_a: Organization
    ) -> None:
        """Subir a hierarquia pareceria esperto e marcaria como esperada uma
        caixa que nada tem a ver com o aviso — e o preço é a equipe não atender
        um rompimento de verdade."""
        _janela(organization_a, scope="OLT", element_id="1")
        assert janela_que_cobre(
            organization_a, scope="CTO", element_external_id="CTO-9",
            moment=timezone.now() - timedelta(minutes=60),
        ) is None

    def test_evento_pontual_nao_e_janela(self, organization_a: Organization) -> None:
        """`ended_at` vazio quer dizer "aconteceu às 14h", não "começou e não sei
        quando acaba" — tratar como janela aberta marcaria como esperada toda
        massiva futura, para sempre."""
        _janela(organization_a, pontual=True)
        assert janela_que_cobre(
            organization_a, scope="OLT", element_external_id="1",
            moment=timezone.now(),
        ) is None

    def test_fora_do_horario_nao_cobre(self, organization_a: Organization) -> None:
        _janela(organization_a, inicio_min_atras=600, duracao_min=60)
        assert janela_que_cobre(
            organization_a, scope="OLT", element_external_id="1",
            moment=timezone.now(),
        ) is None

    def test_evento_de_outro_tipo_nao_e_manutencao(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        inicio = timezone.now() - timedelta(minutes=60)
        EventoRede.objects.create(
            organization=organization_a,
            tipo=EventoRede.Tipo.ROMPIMENTO,
            titulo="Rompimento troncal",
            started_at=inicio,
            ended_at=inicio + timedelta(hours=4),
        )
        assert janela_que_cobre(
            organization_a, scope="OLT", element_external_id="1", moment=timezone.now(),
        ) is None


# =============================================================================
# Fração zero não é medida, é ausência de denominador
# =============================================================================
@pytest.mark.django_db
class TestFracaoSemDenominador:
    def test_massiva_com_gente_fora_e_fracao_zero_nao_diz_zero_por_cento(
        self, organization_a: Organization
    ) -> None:
        """Apareceu em produção: 121 clientes fora e "0% dos logins das caixas
        envolvidas". Eram quedas sem CTO no snapshot — não havia denominador.
        Zero ali se lê como "quase ninguém caiu", o oposto do que aconteceu."""
        set_current_organization(organization_a)
        now = timezone.now()
        outage = OutageEvent.objects.create(
            organization=organization_a,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=1),
            last_detected_at=now,
            scope=OutageEvent.Scope.GEO,
            element_label="Cluster geográfico",
            confidence=OutageEvent.Confidence.LOW,
            affected_count=121,
            restored_count=121,
            affected_fraction=0.0,
        )
        linha = outage_row(outage)
        assert linha["sem_denominador"] is True
        assert "0%" not in linha["elemento_frase"]
        assert "sem denominador" in linha["elemento_frase"]
        assert "quantos caíram" in linha["ressalva_escopo"]

    def test_fracao_de_verdade_continua_aparecendo(
        self, organization_a: Organization
    ) -> None:
        outage = _outage(organization_a)
        linha = outage_row(outage)
        assert linha["sem_denominador"] is False
        assert "40%" in linha["elemento_frase"]


# =============================================================================
# Dispensar a fila do passado (T3 do plano de campo)
# =============================================================================
@pytest.mark.django_db
class TestDispensarCausa:
    """Massiva descartada não é massiva sem resposta.

    Decisão do operador em 21/09/2026: a fila tinha 56 eventos de antes de o
    campo existir, boa parte sem nome (cluster geográfico). Ninguém lembra o que
    foi um evento de duas semanas atrás, e chute vira rótulo errado no treino.

    Elas saem da fila **dispensadas**, com data e motivo — porque no dia em que
    o modelo for treinado, "ninguém respondeu" e "decidimos descartar" são
    informações diferentes.
    """

    def test_dispensada_sai_da_fila(self, organization_a: Organization) -> None:
        from django.core.management import call_command

        _outage(organization_a, inicio_min_atras=60 * 24 * 10)
        assert compute_sem_causa(organization_a)["total"] == 1

        call_command(
            "dispensar_causa_antigas", "acme",
            "--ate", timezone.now().date().isoformat(),
        )
        assert compute_sem_causa(organization_a)["total"] == 0

    def test_o_registro_guarda_data_e_motivo(
        self, organization_a: Organization
    ) -> None:
        from django.core.management import call_command

        outage = _outage(organization_a, inicio_min_atras=60 * 24 * 10)
        call_command(
            "dispensar_causa_antigas", "acme",
            "--ate", timezone.now().date().isoformat(),
        )
        outage.refresh_from_db()
        assert outage.cause_waived is True
        assert "chute vira rótulo errado" in outage.cause_waived_reason
        # E continua sem causa: dispensar não é responder.
        assert outage.confirmed_cause == ""

    def test_nao_toca_no_que_ja_tem_causa(
        self, organization_a: Organization
    ) -> None:
        from django.core.management import call_command

        outage = _outage(
            organization_a, causa=OutageEvent.Cause.ROMPIMENTO,
            inicio_min_atras=60 * 24 * 10,
        )
        call_command(
            "dispensar_causa_antigas", "acme",
            "--ate", timezone.now().date().isoformat(),
        )
        outage.refresh_from_db()
        assert outage.cause_waived_at is None

    def test_massiva_posterior_ao_corte_continua_na_fila(
        self, organization_a: Organization
    ) -> None:
        """O corte é uma data, não "tudo": o que veio depois ainda tem dono e
        memória."""
        from datetime import timedelta as _td

        from django.core.management import call_command

        _outage(organization_a, inicio_min_atras=60)
        ontem = (timezone.now() - _td(days=1)).date().isoformat()
        call_command("dispensar_causa_antigas", "acme", "--ate", ontem)
        assert compute_sem_causa(organization_a)["total"] == 1

    def test_dry_run_nao_grava(self, organization_a: Organization) -> None:
        from django.core.management import call_command

        outage = _outage(organization_a, inicio_min_atras=60 * 24 * 10)
        call_command(
            "dispensar_causa_antigas", "acme",
            "--ate", timezone.now().date().isoformat(), "--dry-run",
        )
        outage.refresh_from_db()
        assert outage.cause_waived_at is None
