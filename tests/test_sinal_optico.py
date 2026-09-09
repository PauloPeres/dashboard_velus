"""Testes de sinal óptico e causa de queda por cliente (#148).

O que está protegido aqui é o que separa esta feature de um gerador de alarme
falso e de uma enxurrada de consultas contra equipamento de produção:

1. **zero não é potência.** `sinal_rx = 0.00` é ausência de leitura (1.391 dos
   4.554 registros em produção). Se ele virasse linha de base, todo cliente que
   caiu apareceria com ~24 dB de perda;
2. **`0000-00-00 00:00:00` é "nunca medido"**, não uma data;
3. **~30% das ONUs não reportam sinal.** Ausência tem que ser guardada como
   ausência, nunca preenchida com zero;
4. **nem toda ONU devolve a causa.** Vazio é "a OLT não informou";
5. **o parser do painel HTML degrada**, nunca levanta — é relatório de tela, o
   formato pode mudar no próximo release do ERP;
6. **sem retorno, zero chamadas à OLT.**

Nada aqui toca no IXC real: tudo passa pelo `FakeOpticalSignalSource`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.fake.optical_signal import FakeOpticalSignalSource
from apps.integrations.ixc.onu_report import onu_report_fields, parse_onu_report_html
from apps.integrations.ixc.schemas import IxcOnuSignalHistorySchema, IxcOnuSignalSchema
from apps.integrations.shared.enums import SourceType
from apps.network.application.connection_poll import run_connection_poll
from apps.network.application.optical_signal import (
    capture_drop_baselines,
    measure_after_restore,
    measure_now_for_connections,
    refresh_optical_signals,
)
from apps.network.domain.dto import ConnectionDTO, OpticalSignalDTO
from apps.network.infrastructure.models import Connection, ConnectionDropEvent
from apps.network.infrastructure.repositories import ConnectionRepository
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource

AGORA = datetime(2026, 9, 8, 19, 46, tzinfo=UTC)

# Painel real capturado em produção (ONU 10733, 2026-09-08), reduzido ao corpo.
# Serve de fixture porque é a única forma honesta de testar um parser de HTML de
# tela: o formato inventado seria o que o parser já sabe ler.
PAINEL_REAL = (
    '<!DOCTYPE html><html><head><meta charset="iso-8859-1"></head>'
    '<body id="ixc_html_body"><div class="panel panel-success">'
    '<div class="panel panel-heading">Pot&#xEA;ncia de ONU</div>'
    '<div class="panel panel-body">'
    "<div>Causa da &#xFA;ltima queda: dying-gasp</div>"
    "<div>Sinal Rx: -18.57</div>"
    "<div>Temperatura: 40</div>"
    "<div>Voltagem: 3.320</div>"
    "<div>Sinal Tx: -23.19</div>"
    "<div>Status pot&#xEA;ncia: Regular</div>"
    "<div>--------------------------------------------------</div>"
    "<div>INFORMA&#xC7;&#xD5;ES ADICIONAIS:</div>"
    "<div>F/s/p: 0/4/3</div>"
    "<div>Run state: online</div>"
    "<div>Ont distance(m): 2369</div>"
    "<div>Mac: 485754435C5A78B9 (HWTC-5C5A78B9)</div>"
    "<div>Last up time: 08-09-2026 19:30:01-03:00</div>"
    "<div>Last dying gasp time: -</div>"
    "</div></div></body></html>"
)


def conexao_dto(
    external_id: str = "1",
    *,
    status: str = "OFFLINE",
    dropped_at: datetime | None = None,
    onu: str = "10733",
) -> ConnectionDTO:
    return ConnectionDTO(
        external_id=external_id,
        customer_external_id=f"cli-{external_id}",
        contract_external_id=f"ctr-{external_id}",
        login=f"cliente{external_id}",
        status=status,
        cto_external_id="1606",
        pon_external_id="449",
        onu_external_id=onu,
        transmitter_external_id="2",
        last_disconnection_at=dropped_at,
    )


def semear_conexao(org: Organization, dto: ConnectionDTO) -> Connection:
    repo = ConnectionRepository(org)
    connection, _ = repo.upsert_from_dto(dto, source_type=SourceType.FAKE)
    return connection


def queda(org: Organization, connection: Connection, *, dropped_at: datetime,
          restored_at: datetime | None = None) -> ConnectionDropEvent:
    return ConnectionDropEvent.objects.create(
        organization=org,
        connection=connection,
        login=connection.login,
        dropped_at=dropped_at,
        restored_at=restored_at,
    )


# ---------------------------------------------------------------------------
# Armadilhas 1 e 2 — a ACL traduzindo ausência
# ---------------------------------------------------------------------------


class TestAusenciaNaACL:
    def test_sinal_zero_vira_ausencia_de_leitura(self) -> None:
        """`0.00` NÃO é 0 dBm — é a ONU que não reportou (1.391 de 4.554)."""
        schema = IxcOnuSignalSchema.model_validate(
            {
                "id": "10736",
                "id_login": "8528",
                "sinal_rx": "0.00",
                "sinal_tx": "0.00",
                "temperatura": "0.00",
                "voltagem": "0.00",
                "data_sinal": "2026-09-08 06:32:28",
                "causa_ultima_queda": "",
            }
        )
        assert schema.sinal_rx is None
        assert schema.sinal_tx is None
        assert schema.temperatura is None
        assert schema.voltagem is None

    def test_data_sinal_zero_date_vira_nulo(self) -> None:
        """`0000-00-00 00:00:00` é o sentinela de "nunca medido" do MySQL."""
        schema = IxcOnuSignalSchema.model_validate(
            {"id": "10736", "sinal_rx": "-24.10", "data_sinal": "0000-00-00 00:00:00"}
        )
        assert schema.data_sinal is None

    def test_causa_traco_e_ausencia_nao_causa(self) -> None:
        """`-` é "a OLT não informou", não uma causa chamada traço."""
        assert (
            IxcOnuSignalSchema.model_validate(
                {"id": "1", "causa_ultima_queda": "-"}
            ).causa_ultima_queda
            == ""
        )
        assert (
            IxcOnuSignalSchema.model_validate(
                {"id": "1", "causa_ultima_queda": "dying-gasp"}
            ).causa_ultima_queda
            == "dying-gasp"
        )

    def test_historico_tambem_zera_para_none(self) -> None:
        row = IxcOnuSignalHistorySchema.model_validate(
            {
                "id": "2557901",
                "id_cliente_fibra": "8948",
                "sinal_rx": "0.00",
                "data_sinal": "2026-09-08 06:32:28",
            }
        )
        assert row.sinal_rx is None

    def test_dto_sem_potencia_nao_tem_sinal(self) -> None:
        """`has_signal` é o que separa "medi" de "tentei medir"."""
        assert not OpticalSignalDTO(onu_external_id="1").has_signal
        assert not OpticalSignalDTO(
            onu_external_id="1", signal_rx=-24.0, measured_at=None
        ).has_signal
        assert OpticalSignalDTO(
            onu_external_id="1", signal_rx=-24.0, measured_at=AGORA
        ).has_signal


# ---------------------------------------------------------------------------
# Armadilha 5 — parser de HTML defensivo
# ---------------------------------------------------------------------------


class TestParserDoPainel:
    def test_le_o_painel_real(self) -> None:
        campos = onu_report_fields(PAINEL_REAL)
        assert campos["signal_rx"] == -18.57
        assert campos["signal_tx"] == -23.19
        assert campos["last_drop_cause"] == "dying-gasp"
        assert campos["run_state"] == "online"
        assert campos["last_up_time"].hour == 19
        # `Last dying gasp time: -` não entra: traço é ausência.
        assert "last_dying_gasp_time" not in campos

    @pytest.mark.parametrize(
        "corpo",
        [
            "",
            None,
            "<html><body>Ocorreu um erro</body></html>",
            "{'json': 'agora o IXC mudou tudo'}",
            "<div>Sinal Rx</div><div>-18.57</div>",  # formato novo, sem "rótulo: valor"
            "<div" * 5000,  # truncado no meio da tag
        ],
    )
    def test_formato_inesperado_degrada_sem_levantar(self, corpo: str | None) -> None:
        """Painel de relatório muda sem aviso. A ACL fica sem leitura, não quebra."""
        assert parse_onu_report_html(corpo) == {}
        assert onu_report_fields(corpo) == {}

    def test_painel_sem_potencia_ainda_entrega_a_causa(self) -> None:
        """ONU que a OLT não achou: sem sinal, mas o que ela disse é notícia."""
        corpo = (
            "<div>Causa da &#xFA;ltima queda: LOSi/LOBi</div>"
            "<div>Sinal Rx: 0.00</div>"
            "<div>Run state: offline</div>"
        )
        campos = onu_report_fields(corpo)
        assert "signal_rx" not in campos  # zero é ausência, some
        assert campos["last_drop_cause"] == "LOSi/LOBi"
        assert campos["run_state"] == "offline"

    def test_rotulo_com_acento_literal_tambem_casa(self) -> None:
        """Se o IXC parar de escapar as entidades, o parser continua achando."""
        campos = onu_report_fields("<td>Causa da última queda: reset</td>")
        assert campos["last_drop_cause"] == "reset"


# ---------------------------------------------------------------------------
# Linha de base — a armadilha central
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLinhaDeBase:
    def test_base_ignora_leitura_zerada_e_busca_a_ultima_valida(
        self, organization_a: Organization
    ) -> None:
        """**A armadilha 1.**

        A ONU parou de reportar (zerou) antes da queda. Se a base fosse "a última
        leitura", o cliente apareceria com ~24 dB de perda. Tem que ser a última
        leitura *válida* — buscada no histórico.
        """
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        # Estado corrente: zerado = sem leitura (é o que a ACL grava como None).
        connection.signal_rx = None
        connection.signal_measured_at = None
        connection.save()

        evento = queda(organization_a, connection, dropped_at=AGORA)

        FakeOpticalSignalSource.set_seed(
            history={
                "10733": [
                    # A mais recente é lixo zerado — não pode ser escolhida.
                    OpticalSignalDTO(
                        onu_external_id="10733",
                        signal_rx=None,
                        measured_at=AGORA - timedelta(hours=13),
                    ),
                    OpticalSignalDTO(
                        onu_external_id="10733",
                        signal_rx=-23.76,
                        measured_at=AGORA - timedelta(days=1),
                    ),
                    OpticalSignalDTO(
                        onu_external_id="10733",
                        signal_rx=-24.43,
                        measured_at=AGORA - timedelta(days=9),
                    ),
                ]
            }
        )

        preenchidos = capture_drop_baselines(
            organization_a, FakeOpticalSignalSource()
        )

        evento.refresh_from_db()
        assert preenchidos == 1
        assert evento.signal_rx_before == -23.76
        assert evento.signal_before_measured_at == AGORA - timedelta(days=1)
        # E, sobretudo: a "perda" não é fictícia.
        evento.signal_rx_after = -23.60
        assert abs(evento.signal_delta_db) < 1.0

    def test_leitura_corrente_valida_e_anterior_a_queda_serve_de_base(
        self, organization_a: Organization
    ) -> None:
        """Varredura diária do IXC: base de até 24 h, sem ir ao histórico."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        connection.signal_rx = -24.10
        connection.signal_measured_at = AGORA - timedelta(hours=13)
        connection.save()
        evento = queda(organization_a, connection, dropped_at=AGORA)

        FakeOpticalSignalSource.set_seed(history={})
        capture_drop_baselines(organization_a, FakeOpticalSignalSource())

        evento.refresh_from_db()
        assert evento.signal_rx_before == -24.10
        # O carimbo é parte do dado: a tela tem que poder dizer "base de 13 h".
        assert evento.signal_before_measured_at == AGORA - timedelta(hours=13)

    def test_leitura_posterior_a_queda_nao_serve_de_base(
        self, organization_a: Organization
    ) -> None:
        """Medição feita depois da queda não é "o sinal com que ele caiu"."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        connection.signal_rx = -30.0
        connection.signal_measured_at = AGORA + timedelta(minutes=5)
        connection.save()
        evento = queda(organization_a, connection, dropped_at=AGORA)

        FakeOpticalSignalSource.set_seed(history={})
        capture_drop_baselines(organization_a, FakeOpticalSignalSource())

        evento.refresh_from_db()
        assert evento.signal_rx_before is None

    def test_onu_sem_historico_fica_sem_base_em_vez_de_zero(
        self, organization_a: Organization
    ) -> None:
        """**Armadilha 3**: ~30% das ONUs não reportam. Ausência é ausência."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        evento = queda(organization_a, connection, dropped_at=AGORA)

        FakeOpticalSignalSource.set_seed(history={})
        preenchidos = capture_drop_baselines(
            organization_a, FakeOpticalSignalSource()
        )

        evento.refresh_from_db()
        assert preenchidos == 0
        assert evento.signal_rx_before is None
        assert evento.signal_before_measured_at is None
        assert evento.signal_delta_db is None


@pytest.mark.django_db
class TestLeituraPassiva:
    def test_refresh_guarda_ausencia_como_ausencia(
        self, organization_a: Organization
    ) -> None:
        """ONU sem potência não vira zero no banco — vira nulo, com causa preservada."""
        set_current_organization(organization_a)
        semear_conexao(organization_a, conexao_dto("1", onu="10736"))

        FakeOpticalSignalSource.set_seed(
            current=[
                OpticalSignalDTO(
                    onu_external_id="10736",
                    login_external_id="1",
                    signal_rx=None,
                    measured_at=None,
                    last_drop_cause="dying-gasp",
                )
            ]
        )
        refresh_optical_signals(organization_a, FakeOpticalSignalSource())

        connection = Connection.objects.get()
        assert connection.signal_rx is None
        assert connection.signal_measured_at is None
        # Armadilha 4: a causa vem do caminho barato — e é guardada literal.
        assert connection.onu_last_drop_cause == "dying-gasp"

    def test_refresh_nao_apaga_run_state_que_so_a_medicao_ativa_sabe(
        self, organization_a: Organization
    ) -> None:
        """A listagem não conhece run state; ausência não pode apagar informação."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        connection.onu_run_state = "online"
        connection.save()

        FakeOpticalSignalSource.set_seed(
            current=[
                OpticalSignalDTO(
                    onu_external_id="10733",
                    login_external_id="1",
                    signal_rx=-24.10,
                    measured_at=AGORA,
                )
            ]
        )
        refresh_optical_signals(organization_a, FakeOpticalSignalSource())

        connection.refresh_from_db()
        assert connection.onu_run_state == "online"
        assert connection.signal_rx == -24.10


# ---------------------------------------------------------------------------
# Acionamento — o coração da decisão de arquitetura
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAcionamento:
    def test_sem_retorno_nenhuma_chamada_sai(
        self, organization_a: Organization
    ) -> None:
        """**A regra que define a feature.** Nada de varredura das ONUs afetadas."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        queda(organization_a, connection, dropped_at=AGORA)  # ainda fora

        resultado = measure_after_restore(
            organization_a, FakeOpticalSignalSource(), event_ids=[], now=AGORA
        )

        assert resultado.measured == 0
        assert FakeOpticalSignalSource.measure_calls() == []

    def test_queda_ainda_aberta_nao_e_medida(
        self, organization_a: Organization
    ) -> None:
        """Só o retorno dispara: cliente ainda fora não vira consulta à OLT."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        aberta = queda(organization_a, connection, dropped_at=AGORA)

        measure_after_restore(
            organization_a,
            FakeOpticalSignalSource(),
            event_ids=[aberta.pk],
            now=AGORA,
        )

        assert FakeOpticalSignalSource.measure_calls() == []

    def test_retorno_dispara_uma_medicao_e_grava_o_depois(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        evento = queda(
            organization_a,
            connection,
            dropped_at=AGORA - timedelta(minutes=30),
            restored_at=AGORA,
        )
        evento.signal_rx_before = -23.76
        evento.signal_before_measured_at = AGORA - timedelta(hours=13)
        evento.save()

        FakeOpticalSignalSource.set_seed(
            measurements={
                "10733": OpticalSignalDTO(
                    onu_external_id="10733",
                    signal_rx=-29.10,
                    measured_at=AGORA + timedelta(minutes=1),
                    run_state="online",
                    last_drop_cause="dying-gasp",
                )
            }
        )
        resultado = measure_after_restore(
            organization_a,
            FakeOpticalSignalSource(),
            event_ids=[evento.pk],
            now=AGORA,
        )

        evento.refresh_from_db()
        connection.refresh_from_db()
        assert resultado.measured == 1
        assert FakeOpticalSignalSource.measure_calls() == ["10733"]
        assert evento.signal_rx_after == -29.10
        assert evento.signal_after_measured_at == AGORA + timedelta(minutes=1)
        # Fusão mal feita: voltou 5,34 dB pior que a linha de base.
        assert evento.signal_delta_db == pytest.approx(-5.34)
        assert connection.onu_run_state == "online"
        assert connection.onu_last_drop_cause == "dying-gasp"

    def test_medicao_e_uma_so_por_queda(
        self, organization_a: Organization
    ) -> None:
        """Retentativa do Celery não vira segunda consulta à OLT."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        evento = queda(
            organization_a, connection, dropped_at=AGORA, restored_at=AGORA
        )
        FakeOpticalSignalSource.set_seed(
            measurements={
                "10733": OpticalSignalDTO(
                    onu_external_id="10733", signal_rx=-24.0, measured_at=AGORA
                )
            }
        )

        for _ in range(3):
            measure_after_restore(
                organization_a,
                FakeOpticalSignalSource(),
                event_ids=[evento.pk],
                now=AGORA,
            )

        assert FakeOpticalSignalSource.measure_calls() == ["10733"]

    def test_onu_desconhecida_pela_olt_conta_como_vazia_sem_inventar_zero(
        self, organization_a: Organization
    ) -> None:
        """De 15 ONUs de logins offline, 10 não devolveram campo nenhum."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        evento = queda(
            organization_a, connection, dropped_at=AGORA, restored_at=AGORA
        )

        FakeOpticalSignalSource.set_seed(measurements={})  # a OLT não conhece
        resultado = measure_after_restore(
            organization_a,
            FakeOpticalSignalSource(),
            event_ids=[evento.pk],
            now=AGORA,
        )

        evento.refresh_from_db()
        assert resultado.measured == 0
        assert resultado.empty == 1
        assert evento.signal_rx_after is None

    def test_teto_por_rodada_limita_as_consultas(
        self, organization_a: Organization, settings
    ) -> None:
        """A OLT é equipamento de produção: teto rígido, não "melhor esforço"."""
        settings.OPTICAL_SIGNAL_MAX_CALLS_PER_ROUND = 2
        set_current_organization(organization_a)

        eventos = []
        medicoes = {}
        for i in range(5):
            conexao = semear_conexao(
                organization_a, conexao_dto(str(i), onu=f"ONU{i}")
            )
            eventos.append(
                queda(organization_a, conexao, dropped_at=AGORA, restored_at=AGORA)
            )
            medicoes[f"ONU{i}"] = OpticalSignalDTO(
                onu_external_id=f"ONU{i}", signal_rx=-24.0, measured_at=AGORA
            )
        FakeOpticalSignalSource.set_seed(measurements=medicoes)

        resultado = measure_after_restore(
            organization_a,
            FakeOpticalSignalSource(),
            event_ids=[e.pk for e in eventos],
            now=AGORA,
        )

        assert resultado.measured == 2
        assert resultado.stopped_early is True
        assert len(FakeOpticalSignalSource.measure_calls()) == 2

    def test_recuo_apos_leituras_vazias_em_serie(
        self, organization_a: Organization
    ) -> None:
        """Vazio em série é sintoma do outro lado — insistir é a pior reação."""
        set_current_organization(organization_a)
        eventos = []
        for i in range(12):
            conexao = semear_conexao(
                organization_a, conexao_dto(str(i), onu=f"ONU{i}")
            )
            eventos.append(
                queda(organization_a, conexao, dropped_at=AGORA, restored_at=AGORA)
            )
        FakeOpticalSignalSource.set_seed(measurements={})  # todas vazias

        resultado = measure_after_restore(
            organization_a,
            FakeOpticalSignalSource(),
            event_ids=[e.pk for e in eventos],
            now=AGORA,
        )

        assert resultado.stopped_early is True
        assert len(FakeOpticalSignalSource.measure_calls()) < 12

    def test_login_sem_onu_casada_e_pulado_sem_chamada(
        self, organization_a: Organization
    ) -> None:
        """97,4% dos logins ativos têm ONU; os outros não têm o que medir."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto(onu=""))
        evento = queda(
            organization_a, connection, dropped_at=AGORA, restored_at=AGORA
        )

        resultado = measure_after_restore(
            organization_a,
            FakeOpticalSignalSource(),
            event_ids=[evento.pk],
            now=AGORA,
        )

        assert resultado.skipped == 1
        assert FakeOpticalSignalSource.measure_calls() == []

    def test_medicao_manual_por_cliente(
        self, organization_a: Organization
    ) -> None:
        """O técnico querendo conferir na hora — sem depender de retorno."""
        set_current_organization(organization_a)
        connection = semear_conexao(organization_a, conexao_dto())
        FakeOpticalSignalSource.set_seed(
            measurements={
                "10733": OpticalSignalDTO(
                    onu_external_id="10733",
                    signal_rx=-18.57,
                    measured_at=AGORA,
                    run_state="online",
                )
            }
        )

        resultado = measure_now_for_connections(
            organization_a,
            FakeOpticalSignalSource(),
            connection_ids=[connection.pk],
            now=AGORA,
        )

        connection.refresh_from_db()
        assert resultado.measured == 1
        assert connection.signal_rx == -18.57
        assert connection.signal_measured_at == AGORA


@pytest.mark.django_db
class TestPollIntegrado:
    def test_o_poll_expoe_quem_voltou_para_disparar_a_medicao(
        self, organization_a: Organization
    ) -> None:
        """O gatilho da medição é o retorno detectado pelo poll — nada mais."""
        set_current_organization(organization_a)
        semear_conexao(organization_a, conexao_dto(status="ONLINE"))

        caido = conexao_dto(dropped_at=AGORA - timedelta(minutes=2))
        FakeConnectionSource.set_seed([caido])
        run_connection_poll(organization_a, FakeConnectionSource(), now=AGORA)

        # Sumiu da lista de offline = voltou.
        FakeConnectionSource.set_seed([])
        resultado = run_connection_poll(
            organization_a, FakeConnectionSource(), now=AGORA + timedelta(minutes=3)
        )

        assert resultado.closed == 1
        assert len(resultado.restored_event_ids) == 1
        evento = ConnectionDropEvent.objects.get()
        assert resultado.restored_event_ids == (evento.pk,)

    def test_poll_sem_ninguem_voltando_nao_lista_retorno(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        semear_conexao(organization_a, conexao_dto(status="ONLINE"))
        FakeConnectionSource.set_seed(
            [conexao_dto(dropped_at=AGORA - timedelta(minutes=2))]
        )

        resultado = run_connection_poll(
            organization_a, FakeConnectionSource(), now=AGORA
        )

        assert resultado.restored_event_ids == ()

    @pytest.mark.e2e
    def test_task_ponta_a_ponta_mede_no_retorno(
        self,
        organization_a: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
        datasource_fake_optical_signal_a: OrganizationDataSource,
        settings,
    ) -> None:
        """Poll → retorno → medição, pelo caminho real das tasks.

        `CELERY_TASK_ALWAYS_EAGER` faz o `apply_async` com countdown rodar em
        linha; o que o teste garante é o encadeamento, não o atraso.
        """
        settings.CELERY_TASK_ALWAYS_EAGER = True
        from apps.network.tasks import poll_connection_status

        set_current_organization(organization_a)
        semear_conexao(organization_a, conexao_dto(status="ONLINE"))
        set_current_organization(None)

        FakeOpticalSignalSource.set_seed(
            measurements={
                "10733": OpticalSignalDTO(
                    onu_external_id="10733",
                    signal_rx=-29.10,
                    measured_at=AGORA,
                    last_drop_cause="dying-gasp",
                )
            }
        )

        FakeConnectionSource.set_seed(
            [conexao_dto(dropped_at=datetime.now(UTC) - timedelta(minutes=2))]
        )
        poll_connection_status(organization_id=organization_a.pk)
        assert FakeOpticalSignalSource.measure_calls() == []  # ninguém voltou

        FakeConnectionSource.set_seed([])
        poll_connection_status(organization_id=organization_a.pk)

        assert FakeOpticalSignalSource.measure_calls() == ["10733"]
        set_current_organization(organization_a)
        evento = ConnectionDropEvent.objects.get()
        assert evento.signal_rx_after == -29.10
