"""Testes do backfill de volume de mensagens (`run_mensagens_backfill`).

Foco no que difere do sync normal:

- grava METADADO, nunca o texto (a decisão de privacidade tem que ser testável,
  não só documentada);
- é idempotente — rerodar o mesmo trecho não duplica nem apaga texto que o
  drill-down já tinha buscado;
- liga a mensagem ao atendimento quando ele existe, e sobrevive quando não;
- reporta o cursor (`ultima_data`) que o chamador transforma em checkpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.atendimento.application.mensagens_backfill import run_mensagens_backfill
from apps.atendimento.domain.dto import MensagemDTO
from apps.atendimento.infrastructure.models import Atendimento, Mensagem
from apps.integrations.fake.atendimento import FakeAtendimentoSource
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


def _msg(
    external_id: str,
    *,
    rota: str = "rota-1",
    direction: str = "AGENT",
    tipo: str = "texto",
    canal: str = "canal-0800",
    fora: bool | None = None,
    dia: int = 1,
) -> MensagemDTO:
    return MensagemDTO(
        external_id=external_id,
        atendimento_external_id=rota,
        direction=direction,
        tipo=tipo,
        texto="",
        sent_at=datetime(2026, 8, dia, 12, 0, tzinfo=UTC),
        canal_external_id=canal,
        fora_janela_24h=fora,
    )


@pytest.fixture
def atendimento_rota_1(db, organization_a: Organization) -> Atendimento:
    set_current_organization(organization_a)
    return Atendimento.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        external_id="rota-1",
        status=Atendimento.Status.CLOSED.value,
        opened_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
    )


@pytest.mark.django_db
class TestRunMensagensBackfill:
    def test_persists_metadata_and_links_atendimento(
        self, organization_a: Organization, atendimento_rota_1: Atendimento
    ) -> None:
        FakeAtendimentoSource.set_seed(
            mensagens_global=[
                _msg("m1", fora=True),
                _msg("m2", direction="CLIENT", tipo="midia", dia=2),
            ]
        )
        source = FakeAtendimentoSource()

        result = run_mensagens_backfill(organization_a, source)

        assert result.mensagens == 2
        set_current_organization(organization_a)
        m1 = Mensagem.objects.get(external_id="m1")
        assert m1.direction == "AGENT"
        assert m1.canal_external_id == "canal-0800"
        assert m1.fora_janela_24h is True
        assert m1.atendimento_id == atendimento_rota_1.id
        assert m1.texto == ""
        assert result.ultima_data == datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    def test_keeps_message_without_known_atendimento(
        self, organization_a: Organization
    ) -> None:
        """Mensagem de conversa não sincronizada entra mesmo assim.

        A listagem global vai mais fundo na história que os atendimentos; jogar
        essas fora tiraria justamente o volume mais antigo do gráfico.
        """
        FakeAtendimentoSource.set_seed(mensagens_global=[_msg("m1", rota="rota-999")])

        result = run_mensagens_backfill(organization_a, FakeAtendimentoSource())

        assert result.mensagens == 1
        set_current_organization(organization_a)
        msg = Mensagem.objects.get(external_id="m1")
        assert msg.atendimento_id is None
        assert msg.atendimento_external_id == "rota-999"

    def test_is_idempotent_and_preserves_drilldown_text(
        self, organization_a: Organization, atendimento_rota_1: Atendimento
    ) -> None:
        set_current_organization(organization_a)
        Mensagem.objects.create(
            organization=organization_a,
            source_type=SourceType.FAKE.value,
            external_id="m1",
            atendimento=atendimento_rota_1,
            atendimento_external_id="rota-1",
            direction="AGENT",
            tipo="texto",
            texto="texto que o drill-down já tinha buscado",
            sent_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        )
        FakeAtendimentoSource.set_seed(mensagens_global=[_msg("m1", canal="canal-novo")])
        source = FakeAtendimentoSource()

        run_mensagens_backfill(organization_a, source)
        run_mensagens_backfill(organization_a, source)

        set_current_organization(organization_a)
        assert Mensagem.objects.filter(external_id="m1").count() == 1
        msg = Mensagem.objects.get(external_id="m1")
        # Metadado novo entra…
        assert msg.canal_external_id == "canal-novo"
        # …mas o texto já buscado não é apagado pelo backfill.
        assert msg.texto == "texto que o drill-down já tinha buscado"

    def test_resumes_from_since_via_binary_search(
        self, organization_a: Organization
    ) -> None:
        FakeAtendimentoSource.set_seed(
            mensagens_global=[
                _msg("m1", dia=1),
                _msg("m2", dia=5),
                _msg("m3", dia=9),
            ]
        )

        result = run_mensagens_backfill(
            organization_a,
            FakeAtendimentoSource(),
            since=datetime(2026, 8, 5, tzinfo=UTC),
        )

        assert result.mensagens == 2
        set_current_organization(organization_a)
        assert set(Mensagem.objects.values_list("external_id", flat=True)) == {
            "m2",
            "m3",
        }

    def test_empty_run_reports_no_cursor(self, organization_a: Organization) -> None:
        """Sem mensagem nova, `ultima_data` fica None — o cursor não pode andar.

        É a lição do #132: checkpoint que avança em rodada vazia transforma
        fonte quebrada em silêncio.
        """
        FakeAtendimentoSource.set_seed(mensagens_global=[])

        result = run_mensagens_backfill(organization_a, FakeAtendimentoSource())

        assert result.mensagens == 0
        assert result.ultima_data is None

    def test_max_pages_reports_incomplete(self, organization_a: Organization) -> None:
        FakeAtendimentoSource.set_seed(
            mensagens_global=[_msg(f"m{i}", dia=1) for i in range(10)]
        )

        result = run_mensagens_backfill(
            organization_a, FakeAtendimentoSource(), page_size=2, max_pages=2
        )

        assert result.mensagens == 4
        assert result.concluido is False
