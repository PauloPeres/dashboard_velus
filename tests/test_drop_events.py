"""Testes dos eventos de queda e da topologia promovida ao login (#143).

Três blocos:
- a derivação de status no adapter (incluindo os valores "SS" e "" do IXC);
- a função pura de diff (`apps.network.application.drop_tracking`), sem banco,
  com a regra de partida a frio;
- a promoção dos campos de topologia a coluna, ponta a ponta com o Fake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.ixc.connections import IxcConnectionSource
from apps.integrations.ixc.schemas import IxcOnuFibraSchema, IxcRadUserSchema
from apps.integrations.shared.enums import Capability, SourceType
from apps.network.application.drop_tracking import (
    ConnectionState,
    OpenDrop,
    diff_drop_events,
)
from apps.network.domain.dto import ConnectionDTO
from apps.network.infrastructure.models import Connection, ConnectionDropEvent
from apps.network.infrastructure.repositories import ConnectionRepository
from apps.shared.context import set_current_organization
from apps.sync.models import SyncMode
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource

AGORA = datetime(2026, 9, 8, 14, 30, tzinfo=UTC)

# Payload real de `radusuarios` (2026-09-08), sem PII.
RAW_RADUSUARIO = {
    "id": "4802", "ativo": "S", "online": "N", "id_contrato": "5173",
    "id_cliente": "7894", "login": "cliente4802", "ip": "",
    "bytes_recebidos": "0", "bytes_enviados": "0",
    "download": "600", "upload": "300",
    "ultima_conexao_inicial": "2026-09-08 20:21:57",
    "ultima_conexao_final": "2026-09-08 20:22:13",
    "motivo_desconexao": "NAS-Request",
    "id_caixa_ftth": "1606", "ftth_porta": "3", "id_transmissor": "2",
    "id_concentrador": "1", "latitude": "-23.5715372", "longitude": "-47.4542107",
    "id_df_projeto": "7",
}

RAW_ONU = {
    "id": "912", "id_login": "4802", "id_radpop_radio_porta": "449",
    "id_caixa_ftth": "1606", "porta_ftth": "3", "id_transmissor": "2",
    "id_contrato": "5173", "onu_tipo": "F", "latitude": "-23.5715372",
    "longitude": "-47.4542107",
}


# =============================================================================
# ACL — o que `radusuarios` diz sobre estar online
# =============================================================================
class TestStatusDoLogin:
    @pytest.mark.parametrize(
        ("ativo", "online", "esperado"),
        [
            ("S", "S", "ONLINE"),
            ("S", "N", "OFFLINE"),
            # "SS" é ausência de sessão registrada, não queda: 31 logins ativos
            # em produção, nenhum com IP, 28 sem nunca ter conectado.
            ("S", "SS", "UNKNOWN"),
            # Campo vazio: 21 logins, última conexão em 2021.
            ("S", "", "UNKNOWN"),
            # Inativo é corte comercial — vence qualquer valor de `online`.
            ("N", "S", "BLOCKED"),
            ("N", "SS", "BLOCKED"),
        ],
    )
    def test_derivacao_de_status(
        self, ativo: str, online: str, esperado: str
    ) -> None:
        schema = IxcRadUserSchema.model_validate(
            {**RAW_RADUSUARIO, "ativo": ativo, "online": online}
        )
        dto = IxcConnectionSource._to_dto(schema)
        assert dto is not None
        assert dto.status == esperado

    def test_topologia_promovida_do_payload(self) -> None:
        schema = IxcRadUserSchema.model_validate(RAW_RADUSUARIO)
        dto = IxcConnectionSource._to_dto(schema, pon_external_id="449")
        assert dto is not None
        assert dto.cto_external_id == "1606"
        assert dto.cto_port == "3"
        assert dto.pon_external_id == "449"
        assert dto.transmitter_external_id == "2"
        assert dto.concentrator_external_id == "1"
        assert dto.latitude == pytest.approx(-23.5715372)
        assert dto.disconnect_reason == "NAS-Request"
        assert dto.last_disconnection_at is not None
        # O que não foi promovido continua opaco em raw_extras.
        assert dto.raw_extras["id_df_projeto"] == "7"

    def test_ids_zerados_viram_vazio(self) -> None:
        schema = IxcRadUserSchema.model_validate(
            {**RAW_RADUSUARIO, "id_caixa_ftth": "0", "id_transmissor": "0"}
        )
        assert schema.id_caixa_ftth == ""
        assert schema.id_transmissor == ""

    def test_schema_da_onu(self) -> None:
        onu = IxcOnuFibraSchema.model_validate(RAW_ONU)
        assert onu.id_login == "4802"
        assert onu.id_radpop_radio_porta == "449"


# =============================================================================
# Função pura de diff — sem Django
# =============================================================================
class TestDiffDeQuedas:
    def _state(self, **over: object) -> ConnectionState:
        base: dict = {
            "key": 1, "status": "OFFLINE", "login": "cliente1",
            # Default do helper: estava no ar da última vez que olhamos, logo é
            # transição de verdade. A partida a frio tem testes próprios.
            "previous_status": "ONLINE",
            "last_disconnection_at": AGORA - timedelta(minutes=5),
            "cto_external_id": "1606", "cto_port": "3",
            "pon_external_id": "449", "transmitter_external_id": "2",
            "latitude": -23.57, "longitude": -47.45,
            "monthly_amount": Decimal("99.90"),
        }
        base.update(over)
        return ConnectionState(**base)  # type: ignore[arg-type]

    def test_offline_novo_abre_queda_com_snapshot(self) -> None:
        diff = diff_drop_events(
            open_drops=[], current=[self._state()], now=AGORA
        )
        assert len(diff.to_open) == 1
        assert not diff.to_close
        queda = diff.to_open[0]
        assert queda.dropped_at == AGORA - timedelta(minutes=5)
        assert queda.cto_external_id == "1606"
        assert queda.pon_external_id == "449"
        assert queda.monthly_amount == Decimal("99.90")

    def test_sem_timestamp_usa_o_instante_do_poll(self) -> None:
        diff = diff_drop_events(
            open_drops=[],
            current=[self._state(last_disconnection_at=None)],
            now=AGORA,
        )
        assert diff.to_open[0].dropped_at == AGORA

    def test_queda_ja_aberta_nao_duplica(self) -> None:
        aberta = OpenDrop(key=1, dropped_at=AGORA - timedelta(minutes=5), event_id=7)
        diff = diff_drop_events(
            open_drops=[aberta], current=[self._state()], now=AGORA
        )
        assert not diff.to_open
        assert not diff.to_close

    def test_online_fecha_a_queda(self) -> None:
        aberta = OpenDrop(key=1, dropped_at=AGORA - timedelta(minutes=30), event_id=7)
        diff = diff_drop_events(
            open_drops=[aberta],
            current=[
                self._state(
                    status="ONLINE",
                    last_connection_at=AGORA - timedelta(minutes=2),
                )
            ],
            now=AGORA,
        )
        assert not diff.to_open
        assert diff.to_close[0].event_id == 7
        assert diff.to_close[0].restored_at == AGORA - timedelta(minutes=2)

    def test_online_sem_inicio_de_sessao_usa_o_poll(self) -> None:
        aberta = OpenDrop(key=1, dropped_at=AGORA - timedelta(minutes=30), event_id=7)
        diff = diff_drop_events(
            open_drops=[aberta],
            current=[self._state(status="ONLINE", last_connection_at=None)],
            now=AGORA,
        )
        assert diff.to_close[0].restored_at == AGORA

    @pytest.mark.parametrize("status", ["BLOCKED", "UNKNOWN"])
    def test_bloqueado_e_desconhecido_nao_abrem_queda(self, status: str) -> None:
        diff = diff_drop_events(
            open_drops=[], current=[self._state(status=status)], now=AGORA
        )
        assert not diff.to_open

    def test_bloqueio_encerra_queda_aberta(self) -> None:
        aberta = OpenDrop(key=1, dropped_at=AGORA - timedelta(hours=2), event_id=7)
        diff = diff_drop_events(
            open_drops=[aberta],
            current=[self._state(status="BLOCKED")],
            now=AGORA,
        )
        assert diff.to_close[0].restored_at == AGORA
        assert not diff.to_open

    def test_nova_queda_depois_de_voltar_entre_polls(self) -> None:
        """Caiu, voltou e caiu de novo sem o poll ver o meio do caminho."""
        aberta = OpenDrop(key=1, dropped_at=AGORA - timedelta(hours=3), event_id=7)
        diff = diff_drop_events(
            open_drops=[aberta],
            current=[
                self._state(
                    last_connection_at=AGORA - timedelta(hours=1),
                    last_disconnection_at=AGORA - timedelta(minutes=4),
                )
            ],
            now=AGORA,
        )
        assert diff.to_close[0].event_id == 7
        assert diff.to_close[0].restored_at == AGORA - timedelta(hours=1)
        assert diff.to_open[0].dropped_at == AGORA - timedelta(minutes=4)

    def test_login_ausente_do_lote_e_ignorado(self) -> None:
        """Sync incremental traz só o delta — quem não veio não muda de estado."""
        aberta = OpenDrop(key=99, dropped_at=AGORA - timedelta(hours=1), event_id=9)
        diff = diff_drop_events(open_drops=[aberta], current=[], now=AGORA)
        assert not diff.to_open
        assert not diff.to_close


# =============================================================================
# Persistência — colunas novas e o model append-only
# =============================================================================
@pytest.mark.django_db
class TestPersistencia:
    def _dto(self, **over: object) -> ConnectionDTO:
        base: dict = {
            "external_id": "4802", "customer_external_id": "7894",
            "contract_external_id": "5173", "login": "cliente4802",
            "status": "OFFLINE", "cto_external_id": "1606", "cto_port": "3",
            "pon_external_id": "449", "transmitter_external_id": "2",
            "concentrator_external_id": "1",
            "latitude": -23.5715372, "longitude": -47.4542107,
            "disconnect_reason": "NAS-Request",
            "last_disconnection_at": AGORA,
        }
        base.update(over)
        return ConnectionDTO(**base)  # type: ignore[arg-type]

    def test_repository_grava_as_colunas(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        conn, _ = ConnectionRepository(organization_a).upsert_from_dto(
            self._dto(), source_type=SourceType.IXC
        )
        conn.refresh_from_db()
        assert conn.cto_external_id == "1606"
        assert conn.pon_external_id == "449"
        assert conn.last_disconnection_at == AGORA
        assert conn.latitude == pytest.approx(-23.5715372)

    def test_evento_de_queda_e_seu_snapshot(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        conn, _ = ConnectionRepository(organization_a).upsert_from_dto(
            self._dto(), source_type=SourceType.IXC
        )
        evento = ConnectionDropEvent.objects.create(
            organization=organization_a,
            connection=conn,
            login=conn.login,
            dropped_at=AGORA,
            cto_external_id=conn.cto_external_id,
            pon_external_id=conn.pon_external_id,
            monthly_amount=Decimal("99.90"),
        )
        assert evento.is_open is True

        # O cliente muda de caixa depois; a massiva de ontem não muda com ele.
        ConnectionRepository(organization_a).upsert_from_dto(
            self._dto(cto_external_id="1607"), source_type=SourceType.IXC
        )
        evento.refresh_from_db()
        assert evento.cto_external_id == "1606"

    def test_uma_queda_aberta_por_conexao(
        self, organization_a: Organization
    ) -> None:
        from django.db.utils import IntegrityError

        set_current_organization(organization_a)
        conn, _ = ConnectionRepository(organization_a).upsert_from_dto(
            self._dto(), source_type=SourceType.IXC
        )
        ConnectionDropEvent.objects.create(
            organization=organization_a, connection=conn,
            login=conn.login, dropped_at=AGORA,
        )
        with pytest.raises(IntegrityError):
            ConnectionDropEvent.objects.create(
                organization=organization_a, connection=conn,
                login=conn.login, dropped_at=AGORA + timedelta(minutes=1),
            )

    def test_quedas_nao_vazam_entre_tenants(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        for org, ext in ((organization_a, "1"), (organization_b, "2")):
            set_current_organization(org)
            conn, _ = ConnectionRepository(org).upsert_from_dto(
                self._dto(external_id=ext), source_type=SourceType.IXC
            )
            ConnectionDropEvent.objects.create(
                organization=org, connection=conn,
                login=f"login{ext}", dropped_at=AGORA,
            )

        set_current_organization(organization_a)
        assert list(
            ConnectionDropEvent.objects.values_list("login", flat=True)
        ) == ["login1"]
        set_current_organization(organization_b)
        assert list(
            ConnectionDropEvent.objects.values_list("login", flat=True)
        ) == ["login2"]


@pytest.mark.django_db
@pytest.mark.e2e
def test_sync_persiste_topologia_do_login(
    organization_a: Organization,
    datasource_fake_connections_a: OrganizationDataSource,
) -> None:
    FakeConnectionSource.set_seed([
        ConnectionDTO(
            external_id="4802", customer_external_id="7894",
            contract_external_id="5173", login="cliente4802", status="OFFLINE",
            cto_external_id="1606", cto_port="3", pon_external_id="449",
            transmitter_external_id="2", latitude=-23.57, longitude=-47.45,
            disconnect_reason="NAS-Request", last_disconnection_at=AGORA,
        )
    ])
    sync_capability(
        organization_id=organization_a.pk,
        capability=Capability.CONNECTIONS.value,
        mode=SyncMode.BOOTSTRAP.value,
    )
    set_current_organization(organization_a)
    conn = Connection.objects.get(external_id="4802")
    assert conn.cto_external_id == "1606"
    assert conn.pon_external_id == "449"
    assert conn.last_disconnection_at == AGORA


# =============================================================================
# Partida a frio — a primeira observação é linha de base, não notícia
# =============================================================================
class TestPartidaAFria:
    """237 logins ativos offline hoje no IXC, muitos caídos há semanas.

    Sem esta regra a primeira rodada do poll inventaria massivas históricas no
    minuto em que a página entrasse no ar.
    """

    BASELINE = AGORA - timedelta(minutes=10)

    def _state(self, **over: object) -> ConnectionState:
        base: dict = {
            "key": 1, "status": "OFFLINE", "login": "cliente1",
            "previous_status": "",
            "last_disconnection_at": AGORA - timedelta(days=21),
        }
        base.update(over)
        return ConnectionState(**base)  # type: ignore[arg-type]

    def test_queda_antiga_na_primeira_observacao_nao_vira_evento(self) -> None:
        diff = diff_drop_events(
            open_drops=[],
            current=[self._state()],
            now=AGORA,
            baseline_at=self.BASELINE,
        )
        assert not diff.to_open

    def test_queda_posterior_ao_baseline_vira_evento(self) -> None:
        diff = diff_drop_events(
            open_drops=[],
            current=[
                self._state(last_disconnection_at=AGORA - timedelta(minutes=3))
            ],
            now=AGORA,
            baseline_at=self.BASELINE,
        )
        assert len(diff.to_open) == 1

    def test_transicao_observada_vale_mesmo_sem_baseline(self) -> None:
        """Vimos o login online e agora está offline: é queda, ponto."""
        diff = diff_drop_events(
            open_drops=[],
            current=[self._state(previous_status="ONLINE")],
            now=AGORA,
            baseline_at=None,
        )
        assert len(diff.to_open) == 1

    def test_sem_baseline_e_sem_transicao_nao_abre(self) -> None:
        diff = diff_drop_events(
            open_drops=[], current=[self._state()], now=AGORA, baseline_at=None
        )
        assert not diff.to_open

    def test_offline_sem_data_de_queda_na_partida_a_frio(self) -> None:
        """Sem `ultima_conexao_final` não há como provar que é queda de agora."""
        diff = diff_drop_events(
            open_drops=[],
            current=[self._state(last_disconnection_at=None)],
            now=AGORA,
            baseline_at=self.BASELINE,
        )
        assert not diff.to_open
