"""IxcConnectionSource — implementação de ConnectionSourcePort para IXC.

Endpoint IXC: `radusuarios`. Status derivado de ativo/online:
- ativo=N            -> BLOCKED
- ativo=S & online=S -> ONLINE
- ativo=S & online=N -> OFFLINE
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import ConnectionDTO

from .client import IxcHttpClient
from .schemas import IxcRadUserSchema

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

                dto = self._to_dto(schema)
                if dto is not None:
                    yield dto

            if skipped:
                _logger.info("ixc_connection_list_done", skipped=skipped)

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
                return self._to_dto(schema)
        return None

    @staticmethod
    def _to_dto(schema: IxcRadUserSchema) -> ConnectionDTO | None:
        if not schema.is_active:
            status = "BLOCKED"
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
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "qtype": "radusuarios.ultima_conexao",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
