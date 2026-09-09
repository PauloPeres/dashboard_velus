"""Testes do poll de status de conexão a cada 3 min (#144).

O que está sendo protegido aqui é o que a ferramenta promete quando estreia:
não inventar queda na primeira rodada (partida a frio), fechar a queda de quem
sumiu da lista de offline (o retorno não vem como registro, vem como ausência)
e **não concluir nada** quando a leitura do IXC falha — que acontece de verdade,
o host é dual-stack sem rota IPv6 no cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.shared.enums import SourceType
from apps.network.application.connection_poll import run_connection_poll
from apps.network.domain.dto import ConnectionDTO
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    ConnectionPollState,
)
from apps.network.infrastructure.repositories import ConnectionRepository
from apps.network.tasks import poll_connection_status
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource

AGORA = datetime(2026, 9, 8, 19, 46, tzinfo=UTC)


def dto(
    external_id: str,
    *,
    status: str = "OFFLINE",
    dropped_at: datetime | None = None,
    cto: str = "1606",
    contrato: str = "",
) -> ConnectionDTO:
    return ConnectionDTO(
        external_id=external_id,
        customer_external_id=f"cli-{external_id}",
        contract_external_id=contrato or f"ctr-{external_id}",
        login=f"cliente{external_id}",
        status=status,
        cto_external_id=cto,
        cto_port="3",
        pon_external_id="449",
        transmitter_external_id="2",
        latitude=-23.57,
        longitude=-47.45,
        disconnect_reason="NAS-Request",
        last_disconnection_at=dropped_at,
    )


def semear(org: Organization, dtos: list[ConnectionDTO]) -> None:
    """Grava o estado que o sync de 6h teria deixado no banco."""
    repo = ConnectionRepository(org)
    for item in dtos:
        repo.upsert_from_dto(item, source_type=SourceType.FAKE)


def poll(org: Organization, offline: list[ConnectionDTO], *, now: datetime):
    FakeConnectionSource.set_seed(offline)
    return run_connection_poll(org, FakeConnectionSource(), now=now)


@pytest.mark.django_db
class TestPollDeStatus:
    def test_partida_a_frio_nao_inventa_queda(
        self, organization_a: Organization
    ) -> None:
        """Login offline desde agosto na primeira rodada é linha de base, não notícia."""
        set_current_organization(organization_a)
        antigo = dto("1", dropped_at=AGORA - timedelta(days=21))
        semear(organization_a, [antigo])

        resultado = poll(organization_a, [antigo], now=AGORA)

        assert resultado.opened == 0
        assert ConnectionDropEvent.objects.count() == 0
        assert ConnectionPollState.objects.get().baseline_at == AGORA

    def test_transicao_observada_abre_queda(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        semear(organization_a, [dto("1", status="ONLINE")])

        caido = dto("1", dropped_at=AGORA - timedelta(minutes=2))
        resultado = poll(organization_a, [caido], now=AGORA)

        assert resultado.opened == 1
        evento = ConnectionDropEvent.objects.get()
        assert evento.dropped_at == AGORA - timedelta(minutes=2)
        # Snapshot da topologia no instante da queda.
        assert evento.cto_external_id == "1606"
        assert evento.pon_external_id == "449"
        assert Connection.objects.get().status == Connection.Status.OFFLINE

    def test_queda_ja_aberta_nao_duplica_entre_polls(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        semear(organization_a, [dto("1", status="ONLINE")])
        caido = dto("1", dropped_at=AGORA - timedelta(minutes=2))

        poll(organization_a, [caido], now=AGORA)
        poll(organization_a, [caido], now=AGORA + timedelta(minutes=3))

        assert ConnectionDropEvent.objects.count() == 1

    def test_sumir_da_lista_de_offline_fecha_a_queda(
        self, organization_a: Organization
    ) -> None:
        """O poll só lista quem está fora: o retorno chega como ausência."""
        set_current_organization(organization_a)
        semear(organization_a, [dto("1", status="ONLINE")])
        caido = dto("1", dropped_at=AGORA - timedelta(minutes=2))
        poll(organization_a, [caido], now=AGORA)

        depois = AGORA + timedelta(minutes=3)
        resultado = poll(organization_a, [], now=depois)

        assert resultado.closed == 1
        evento = ConnectionDropEvent.objects.get()
        assert evento.restored_at == depois
        # O status corrente NÃO é reescrito pra ONLINE: sumir da lista não prova
        # que voltou (pode ter sido bloqueado no ERP) e o poll não leu o
        # registro. Quem responde "está fora agora" é a queda aberta.
        assert Connection.objects.get().status == Connection.Status.OFFLINE

    def test_pon_do_banco_sobrevive_ao_poll(
        self, organization_a: Organization
    ) -> None:
        """O poll não paga o enriquecimento de ONU — não pode apagar a PON do sync."""
        set_current_organization(organization_a)
        semear(organization_a, [dto("1", status="ONLINE")])

        sem_pon = ConnectionDTO(
            external_id="1",
            customer_external_id="cli-1",
            contract_external_id="ctr-1",
            login="cliente1",
            status="OFFLINE",
            cto_external_id="1606",
            last_disconnection_at=AGORA - timedelta(minutes=2),
        )
        poll(organization_a, [sem_pon], now=AGORA)

        assert Connection.objects.get().pon_external_id == "449"

    def test_contrato_cancelado_nao_conta_como_queda(
        self, organization_a: Organization
    ) -> None:
        """Corte comercial não é falha de rede (§5.6)."""
        from apps.customers.infrastructure.models import Contract

        set_current_organization(organization_a)
        Contract.objects.create(
            organization=organization_a,
            source_type=SourceType.FAKE.value,
            external_id="ctr-1",
            customer_external_id="cli-1",
            plan_name="Fibra 500M",
            monthly_amount=100,
            status=Contract.Status.CANCELED,
        )
        semear(organization_a, [dto("1", status="ONLINE", contrato="ctr-1")])

        resultado = poll(
            organization_a,
            [dto("1", dropped_at=AGORA - timedelta(minutes=2), contrato="ctr-1")],
            now=AGORA,
        )

        assert resultado.opened == 0
        assert ConnectionDropEvent.objects.count() == 0


@pytest.mark.django_db
class TestLeituraQueFalha:
    """Uma rodada ruim não pode virar notícia — nem de queda, nem de retorno."""

    def test_falha_na_leitura_nao_abre_nem_fecha_nada(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        semear(organization_a, [dto("1", status="ONLINE")])
        poll(
            organization_a,
            [dto("1", dropped_at=AGORA - timedelta(minutes=2))],
            now=AGORA,
        )

        FakeConnectionSource.fail_offline_listing(
            RuntimeError("IXC resposta não-JSON (provável erro server-side)")
        )
        try:
            resultado = poll(organization_a, [], now=AGORA + timedelta(minutes=3))
        finally:
            FakeConnectionSource.fail_offline_listing(None)

        assert resultado.failed is True
        assert resultado.opened == 0
        assert resultado.closed == 0
        # A queda continua aberta: "não veio na lista" só significa "voltou"
        # quando a lista veio.
        assert ConnectionDropEvent.objects.get().restored_at is None

    def test_a_task_nao_levanta_quando_o_ixc_falha(
        self,
        organization_a: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
    ) -> None:
        FakeConnectionSource.fail_offline_listing(ConnectionError("Errno 101"))
        try:
            resultado = poll_connection_status(organization_id=organization_a.pk)
        finally:
            FakeConnectionSource.fail_offline_listing(None)

        assert resultado["failed"] is True


@pytest.mark.django_db
@pytest.mark.e2e
def test_task_ponta_a_ponta(
    organization_a: Organization,
    datasource_fake_connections_a: OrganizationDataSource,
) -> None:
    set_current_organization(organization_a)
    semear(organization_a, [dto("1", status="ONLINE")])
    set_current_organization(None)

    FakeConnectionSource.set_seed([dto("1", dropped_at=AGORA - timedelta(minutes=2))])
    resultado = poll_connection_status(organization_id=organization_a.pk)

    assert resultado["failed"] is False
    assert resultado["opened"] == 1

    set_current_organization(organization_a)
    assert ConnectionDropEvent.objects.count() == 1
