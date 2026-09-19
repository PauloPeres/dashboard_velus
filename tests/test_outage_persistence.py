"""Persistência e reconciliação de massivas (#145/#147).

O detector é sem estado e roda a cada 3 min sobre as quedas abertas — a mesma
massiva volta a ser detectada rodada após rodada, com mais gente a cada vez. O
que estes testes protegem é a costura: ela tem que **crescer**, não virar uma
massiva nova a cada poll; tem que registrar o retorno cliente a cliente; e tem
que se encerrar sozinha quando ≥90% voltou, sem ressuscitar depois.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from apps.customers.infrastructure.models import Contract
from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.shared.enums import SourceType
from apps.network.application.connection_poll import run_connection_poll
from apps.network.domain.dto import ConnectionDTO
from apps.network.infrastructure.models import (
    ConnectionDropEvent,
    NetworkElement,
    OutageAffectedLogin,
    OutageEvent,
)
from apps.network.infrastructure.repositories import ConnectionRepository
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

AGORA = datetime(2026, 9, 8, 19, 46, tzinfo=UTC)

# A caixa tem 6 logins ativos: 5 fora já passam o limiar de 70% e o mínimo de 5
# clientes, com denominador acima do piso de evidência (§2.5b).
CTO = "1606"
LOGINS = [str(n) for n in range(1, 7)]


def dto(
    external_id: str,
    *,
    status: str = "OFFLINE",
    dropped_at: datetime | None = None,
    cto: str = CTO,
) -> ConnectionDTO:
    return ConnectionDTO(
        external_id=external_id,
        customer_external_id=f"cli-{external_id}",
        contract_external_id=f"ctr-{external_id}",
        login=f"cliente{external_id}",
        status=status,
        cto_external_id=cto,
        cto_port=external_id,
        pon_external_id="449",
        transmitter_external_id="2",
        latitude=-23.57,
        longitude=-47.45,
        last_disconnection_at=dropped_at,
    )


@pytest.fixture
def rede(organization_a: Organization) -> Organization:
    """Seis logins online na caixa 1606, com contrato ativo e planta cadastrada."""
    set_current_organization(organization_a)
    NetworkElement.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        kind=NetworkElement.Kind.CTO,
        external_id=CTO,
        name="B41-SP03",
        parent_kind=NetworkElement.Kind.OLT,
        parent_external_id="2",
        latitude=-23.57,
        longitude=-47.45,
    )
    repo = ConnectionRepository(organization_a)
    for login in LOGINS:
        Contract.objects.create(
            organization=organization_a,
            source_type=SourceType.FAKE.value,
            external_id=f"ctr-{login}",
            customer_external_id=f"cli-{login}",
            plan_name="Fibra 500M",
            monthly_amount=Decimal("100.00"),
            status=Contract.Status.ACTIVE,
        )
        repo.upsert_from_dto(dto(login, status="ONLINE"), source_type=SourceType.FAKE)
    return organization_a


def poll(org: Organization, offline: list[ConnectionDTO], *, now: datetime):
    FakeConnectionSource.set_seed(offline)
    return run_connection_poll(org, FakeConnectionSource(), now=now)


def caidos(logins: list[str], *, dropped_at: datetime) -> list[ConnectionDTO]:
    return [dto(login, dropped_at=dropped_at) for login in logins]


@pytest.mark.django_db
class TestMassivaQueCresce:
    def test_a_mesma_massiva_cresce_entre_polls(self, rede: Organization) -> None:
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        assert OutageEvent.objects.count() == 1
        massiva = OutageEvent.objects.get()
        assert massiva.scope == OutageEvent.Scope.CTO
        assert massiva.affected_count == 5
        assert massiva.element_label == "B41-SP03"

        # Três minutos depois, o sexto cliente da mesma caixa cai.
        depois = AGORA + timedelta(minutes=3)
        offline = caidos(LOGINS[:5], dropped_at=AGORA) + caidos(
            LOGINS[5:], dropped_at=depois - timedelta(minutes=1)
        )
        poll(rede, offline, now=depois)

        assert OutageEvent.objects.count() == 1
        massiva.refresh_from_db()
        assert massiva.affected_count == 6
        assert OutageAffectedLogin.objects.filter(outage=massiva).count() == 6
        # MRR afetado acompanha o crescimento.
        assert massiva.mrr_at_risk == Decimal("600.00")

    def test_redetectar_a_mesma_queda_nao_duplica_o_afetado(
        self, rede: Organization
    ) -> None:
        for minuto in (0, 3, 6):
            poll(
                rede,
                caidos(LOGINS[:5], dropped_at=AGORA),
                now=AGORA + timedelta(minutes=minuto),
            )

        assert OutageEvent.objects.count() == 1
        assert OutageAffectedLogin.objects.count() == 5

    def test_massiva_guarda_escopo_e_fracao_separados(
        self, rede: Organization
    ) -> None:
        """A UI precisa dos fatos soltos: elemento e fração não vêm costurados (§8)."""
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        massiva = OutageEvent.objects.get()
        assert massiva.affected_fraction == pytest.approx(5 / 6)
        assert massiva.suspected_element is not None
        assert massiva.suspected_element.external_id == CTO


@pytest.mark.django_db
class TestRetornoEEncerramento:
    def test_retorno_por_cliente_vira_progresso(self, rede: Organization) -> None:
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        volta = AGORA + timedelta(minutes=3)
        poll(rede, caidos(LOGINS[:3], dropped_at=AGORA), now=volta)

        massiva = OutageEvent.objects.get()
        assert massiva.affected_count == 5
        assert massiva.restored_count == 2
        assert massiva.ended_at is None
        restaurados = OutageAffectedLogin.objects.filter(restored_at=volta)
        assert restaurados.count() == 2

    def test_abaixo_de_90_por_cento_nao_encerra(self, rede: Organization) -> None:
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        # 4 de 5 voltaram = 80%.
        poll(rede, caidos(LOGINS[:1], dropped_at=AGORA), now=AGORA + timedelta(minutes=3))

        massiva = OutageEvent.objects.get()
        assert massiva.restored_count == 4
        assert massiva.ended_at is None

    def test_encerra_sozinha_quando_todos_voltam(self, rede: Organization) -> None:
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        fim = AGORA + timedelta(minutes=6)
        poll(rede, [], now=fim)

        massiva = OutageEvent.objects.get()
        assert massiva.restored_count == 5
        assert massiva.ended_at == fim

    def test_massiva_encerrada_nao_ressuscita(self, rede: Organization) -> None:
        """Os que não voltaram não podem virar massiva nova a cada 3 min."""
        poll(rede, caidos(LOGINS, dropped_at=AGORA), now=AGORA)
        assert OutageEvent.objects.get().affected_count == 6

        # 5 de 6 voltam (83%) e depois mais um cliente da caixa cai de novo?
        # Não: aqui só o retorno interessa — 6 de 6 encerra.
        poll(rede, [], now=AGORA + timedelta(minutes=6))
        massiva = OutageEvent.objects.get()
        assert massiva.ended_at is not None

        # Uma queda antiga reaberta pela mão (o cliente que nunca voltou) não
        # pode gerar um evento novo a partir da massiva já encerrada.
        ConnectionDropEvent.objects.update(restored_at=None)
        poll(rede, caidos(LOGINS, dropped_at=AGORA), now=AGORA + timedelta(minutes=9))

        assert OutageEvent.objects.count() == 1
        assert OutageEvent.objects.get().ended_at is not None


@pytest.mark.django_db
def test_queda_isolada_nao_vira_massiva(rede: Organization) -> None:
    """Registra a queda, não inventa massiva (§5.6)."""
    poll(rede, caidos(LOGINS[:2], dropped_at=AGORA), now=AGORA)

    assert ConnectionDropEvent.objects.count() == 2
    assert OutageEvent.objects.count() == 0


@pytest.mark.django_db
def test_leitura_falha_nao_cria_massiva(rede: Organization) -> None:
    FakeConnectionSource.fail_offline_listing(RuntimeError("página HTML de erro"))
    try:
        resultado = poll(rede, caidos(LOGINS, dropped_at=AGORA), now=AGORA)
    finally:
        FakeConnectionSource.fail_offline_listing(None)

    assert resultado.failed is True
    assert OutageEvent.objects.count() == 0
    assert ConnectionDropEvent.objects.count() == 0


@pytest.mark.django_db
class TestManutencaoProgramada:
    """A massiva que nasce dentro de uma janela avisada (R10).

    Sem isto, toda janela programada vira "massiva": envenena a estatística de
    disponibilidade e treina a equipe a ignorar alarme. A janela é o evento de
    rede que a equipe já cadastra na aba de Tendências — um cadastro só, porque
    dois garantiriam o dia em que alguém avisa num lugar e a massiva alarma do
    outro.
    """

    def _janela(
        self, org: Organization, *, scope: str = "", element_id: str = ""
    ) -> Any:
        from apps.atendimento.infrastructure.models import EventoRede

        set_current_organization(org)
        return EventoRede.objects.create(
            organization=org,
            tipo=EventoRede.Tipo.MANUTENCAO,
            titulo="Troca de cordoalha na CX 1606",
            started_at=AGORA - timedelta(hours=1),
            ended_at=AGORA + timedelta(hours=3),
            scope=scope,
            element_external_id=element_id,
        )

    def test_massiva_dentro_da_janela_nasce_marcada(self, rede: Organization) -> None:
        janela = self._janela(rede, scope=OutageEvent.Scope.CTO, element_id=CTO)
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        massiva = OutageEvent.objects.get()
        assert massiva.maintenance_event_id == janela.pk
        assert massiva.is_expected is True
        assert massiva.maintenance_label == "Troca de cordoalha na CX 1606"

    def test_janela_de_outro_elemento_nao_marca(self, rede: Organization) -> None:
        self._janela(rede, scope=OutageEvent.Scope.CTO, element_id="outra-caixa")
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)

        massiva = OutageEvent.objects.get()
        assert massiva.is_expected is False

    def test_janela_cadastrada_depois_nao_marca_retroativamente(
        self, rede: Organization
    ) -> None:
        """O registro guarda o que se sabia quando a massiva apareceu.

        Reescrever isso apagaria a diferença entre "avisamos antes" e
        "explicamos depois" — que é justamente o que a janela existe para medir.
        """
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=AGORA)
        self._janela(rede, scope=OutageEvent.Scope.CTO, element_id=CTO)

        # Segundo poll, com a massiva já aberta: continua não marcada.
        depois = AGORA + timedelta(minutes=3)
        poll(rede, caidos(LOGINS[:5], dropped_at=AGORA), now=depois)

        massiva = OutageEvent.objects.get()
        assert massiva.is_expected is False
