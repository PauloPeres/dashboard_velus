"""IxcBandwidthUsageSource — implementação de BandwidthUsageSourcePort para IXC.

Endpoint IXC: `radusuarios_consumo` (accounting RADIUS por cliente/período).
Cada registro é o consumo acumulado de um cliente num período. Volume alto —
sync incremental filtra por `data` a partir do último checkpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import BandwidthUsageDTO

from .client import IxcHttpClient
from .schemas import IxcBandwidthSchema

_logger = structlog.get_logger(__name__)


class IxcBandwidthUsageSource:
    """Adapter IXC para a capability BANDWIDTH."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.BANDWIDTH})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_bandwidth_usage(
        self, *, since: datetime | None = None
    ) -> Iterator[BandwidthUsageDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_ixc(
                "radusuarios_consumo", body_filter=body_filter
            ):
                try:
                    schema = IxcBandwidthSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_bandwidth_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield self._to_dto(schema)

            if skipped:
                _logger.info("ixc_bandwidth_list_done", skipped=skipped)

    def get_bandwidth_usage(self, external_id: str) -> BandwidthUsageDTO | None:
        with self._client_factory() as client:
            body_filter = {
                "qtype": "radusuarios_consumo.id",
                "query": external_id,
                "oper": "=",
            }
            for raw in client.paginate_ixc(
                "radusuarios_consumo", body_filter=body_filter
            ):
                try:
                    schema = IxcBandwidthSchema.model_validate(raw)
                except ValidationError:
                    return None
                return self._to_dto(schema)
        return None

    @staticmethod
    def _to_dto(schema: IxcBandwidthSchema) -> BandwidthUsageDTO:
        return BandwidthUsageDTO(
            external_id=schema.id,
            customer_external_id=schema.id_cliente,
            download_bytes=schema.acctinputoctets,
            upload_bytes=schema.acctoutputoctets,
            session_time=schema.acctsessiontime,
            reference_date=schema.data,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "qtype": "radusuarios_consumo.data",
            "query": sp.strftime("%Y-%m-%d"),
            "oper": ">=",
        }
