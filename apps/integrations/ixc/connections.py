"""IxcConnectionSource — implementação de ConnectionSourcePort para IXC.

Endpoint IXC: `radusuarios`. Status derivado de ativo/online:
- ativo=N              -> BLOCKED
- ativo=S & online=S   -> ONLINE
- ativo=S & online=N   -> OFFLINE
- ativo=S & online=SS/"" -> UNKNOWN (ver IxcRadUserSchema.has_known_session_state)

Enriquecido com `radpop_radio_cliente_fibra` (o registro da ONU) pra obter a
porta PON do login — é o degrau entre CTO e OLT na escada de escopo do detector
de massivas, e não existe em `radusuarios`.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import ConnectionDTO

from .client import IxcHttpClient
from .schemas import IxcOnuFibraSchema, IxcRadUserSchema

_logger = structlog.get_logger(__name__)


class IxcConnectionSource:
    """Adapter IXC para a capability CONNECTIONS."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CONNECTIONS})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_connections(
        self, *, since: datetime | None = None
    ) -> Iterator[ConnectionDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            # Uma chamada pra planta de ONUs antes de paginar os logins: 4.559
            # linhas, uma vez por sync, contra uma consulta por login. O mapa
            # cabe folgado em memória.
            onu_by_login = self._fetch_onu_by_login(client)

            skipped = 0
            for raw in client.paginate_ixc("radusuarios", body_filter=body_filter):
                try:
                    schema = IxcRadUserSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_radusuario_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue

                pon, onu_id = onu_by_login.get(schema.id, ("", ""))
                dto = self._to_dto(
                    schema, pon_external_id=pon, onu_external_id=onu_id
                )
                if dto is not None:
                    yield dto

            if skipped:
                _logger.info("ixc_connection_list_done", skipped=skipped)

    def list_offline_connections(self) -> Iterator[ConnectionDTO]:
        """Logins ativos com `online=N` — a leitura barata do poll de 3 min (#144).

        O filtro principal vai em `qtype/query/oper` (`radusuarios.online = N`) e
        o recorte de contrato ativo em `grid_param`, que é como o IXC aceita um
        segundo critério na mesma consulta. Medido em produção (2026-09-08):
        ~237 linhas numa chamada — barato de propósito, é o que permite a
        cadência de minutos.

        Sem o enriquecimento de PON: são 4.559 linhas de ONU por rodada pra
        acrescentar um campo que o `Connection` já tem do sync de 6h. O poll
        preserva a PON que já está no banco (ver `connection_poll`).
        """
        body_filter = {
            "qtype": "radusuarios.online",
            "query": "N",
            "oper": "=",
            "grid_param": _json.dumps(
                [{"TB": "radusuarios.ativo", "OP": "=", "P": "S"}]
            ),
        }
        with self._client_factory() as client:
            for raw in client.paginate_ixc("radusuarios", body_filter=body_filter):
                try:
                    schema = IxcRadUserSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "ixc_radusuario_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                dto = self._to_dto(schema)
                if dto is not None:
                    yield dto

    def get_connection(self, external_id: str) -> ConnectionDTO | None:
        with self._client_factory() as client:
            body_filter = {
                "qtype": "radusuarios.id",
                "query": external_id,
                "oper": "=",
            }
            for raw in client.paginate_ixc("radusuarios", body_filter=body_filter):
                try:
                    schema = IxcRadUserSchema.model_validate(raw)
                except ValidationError:
                    return None
                # Consulta pontual da ONU — aqui o mapa inteiro não se paga.
                onu = self._fetch_onu_by_login(client, login_external_id=external_id)
                pon, onu_id = onu.get(schema.id, ("", ""))
                return self._to_dto(
                    schema, pon_external_id=pon, onu_external_id=onu_id
                )
        return None

    @staticmethod
    def _fetch_onu_by_login(
        client: IxcHttpClient, *, login_external_id: str | None = None
    ) -> dict[str, tuple[str, str]]:
        """Mapa `radusuarios.id` → (id da porta PON, id da ONU), de `radpop_radio_cliente_fibra`.

        O id da ONU vem junto porque **a mesma leitura já o traz**: é a chave do
        disparo de medição de potência (#148), e buscá-lo depois custaria uma
        segunda varredura de 4.559 linhas pra descobrir o que estava aqui.

        Por que a PON vem daqui e não de `rad_caixa_ftth.id_interface` (medido em
        produção 2026-09-08): o campo da caixa está preenchido em 420 das 1.445
        CTOs, contra 4.553 de 4.559 no registro da ONU. E, sobretudo, PON é
        propriedade do login e não da caixa — derivando CTO → PON pelos clientes,
        239 das 927 caixas deriváveis (26%) apontam pra mais de uma porta. Um
        mapa CTO → PON estaria errado em um quarto dos casos. Não "simplifique"
        pro campo da caixa.

        O join foi verificado: `id_login` casa com `radusuarios.id` em 4.081 de
        4.081 linhas úteis, cobrindo 3.323 dos 3.413 logins ativos (97,4%) e
        3.096 dos 3.125 online (99,1%). Nenhum login aponta pra 2 PONs.
        """
        body_filter = {
            "qtype": "radpop_radio_cliente_fibra.id_login",
            "query": login_external_id,
            "oper": "=",
        } if login_external_id else {
            "qtype": "radpop_radio_cliente_fibra.id",
            "query": "1",
            "oper": ">=",
        }

        mapping: dict[str, tuple[str, str]] = {}
        try:
            rows = client.paginate_ixc(
                "radpop_radio_cliente_fibra", body_filter=body_filter
            )
            for raw in rows:
                try:
                    onu = IxcOnuFibraSchema.model_validate(raw)
                except ValidationError:
                    continue
                if onu.id_login:
                    mapping[onu.id_login] = (onu.id_radpop_radio_porta, onu.id)
        except Exception:
            # Enriquecimento: se a planta de ONUs falhar, o sync de conexões não
            # pode cair junto — o estado do login (que é o dado principal) segue
            # entrando, só sem a PON.
            _logger.warning("ixc_onu_fibra_fetch_failed", exc_info=True)
            return {}

        _logger.info("ixc_onu_fibra_map_ready", logins=len(mapping))
        return mapping

    @staticmethod
    def _to_dto(
        schema: IxcRadUserSchema,
        *,
        pon_external_id: str = "",
        onu_external_id: str = "",
    ) -> ConnectionDTO | None:
        if not schema.is_active:
            status = "BLOCKED"
        elif not schema.has_known_session_state:
            # "SS" e "" não são queda — são ausência de sessão registrada.
            # O raciocínio e os números estão em
            # IxcRadUserSchema.has_known_session_state; não colapse em != "S".
            status = "UNKNOWN"
        elif schema.is_online:
            status = "ONLINE"
        else:
            status = "OFFLINE"

        return ConnectionDTO(
            external_id=schema.id,
            customer_external_id=schema.id_cliente,
            contract_external_id=schema.id_contrato,
            login=schema.login,
            status=status,
            ip=schema.ip,
            nas_ip=schema.nas_ip,
            rx_bytes=schema.bytes_recebidos,
            tx_bytes=schema.bytes_enviados,
            download_speed=schema.download,
            upload_speed=schema.upload,
            last_connection_at=schema.ultima_conexao,
            cto_external_id=schema.id_caixa_ftth,
            cto_port=schema.ftth_porta,
            pon_external_id=pon_external_id,
            onu_external_id=onu_external_id,
            transmitter_external_id=schema.id_transmissor,
            concentrator_external_id=schema.id_concentrador,
            latitude=schema.latitude,
            longitude=schema.longitude,
            disconnect_reason=schema.motivo_desconexao,
            last_disconnection_at=schema.ultima_conexao_final,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        # `ultima_conexao` deixou de existir como coluna no radusuarios (o IXC
        # renomeou p/ ultima_conexao_inicial/final) e o filtro passou a devolver
        # página HTML de erro. `ultima_atualizacao` é o last-modified da linha —
        # pega qualquer mudança (status incluso), melhor que só novas conexões.
        return {
            "qtype": "ultima_atualizacao",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
