"""IxcEquipmentSource — implementação de EquipmentSourcePort para IXC.

Endpoint IXC: `cliente_contrato_comodato` (equipamentos em comodato por contrato).
Cada registro é um equipamento (ONT, roteador, switch) emprestado ao cliente,
atrelado a um contrato via `id_cliente_contrato`.

Status IXC mapeado: A (ativo/em campo) -> ACTIVE, D (devolvido) -> RETURNED.
Demais valores -> UNKNOWN (valor cru preservado em raw_extras).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.inventory.domain.dto import EquipmentDTO

from .client import IxcHttpClient
from .schemas import IxcEquipmentSchema

_logger = structlog.get_logger(__name__)

# Mapeia o status do comodato IXC pro status canônico do EquipmentDTO.
_STATUS_MAP: dict[str, str] = {
    "A": "ACTIVE",
    "S": "ACTIVE",
    "D": "RETURNED",
    "N": "RETURNED",
}


class IxcEquipmentSource:
    """Adapter IXC para a capability EQUIPMENT."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.EQUIPMENT})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_equipment(
        self, *, since: datetime | None = None
    ) -> Iterator[EquipmentDTO]:
        # O endpoint de comodato não tem campo de data confiável pra incremental;
        # sempre faz full scan. O upsert idempotente cuida da reconciliação.
        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_ixc("cliente_contrato_comodato"):
                try:
                    schema = IxcEquipmentSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_comodato_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue

                dto = self._to_dto(schema)
                if dto is not None:
                    yield dto

            if skipped:
                _logger.info("ixc_equipment_list_done", skipped=skipped)

    def get_equipment(self, external_id: str) -> EquipmentDTO | None:
        with self._client_factory() as client:
            body_filter = {
                "qtype": "cliente_contrato_comodato.id",
                "query": external_id,
                "oper": "=",
            }
            for raw in client.paginate_ixc(
                "cliente_contrato_comodato", body_filter=body_filter
            ):
                try:
                    schema = IxcEquipmentSchema.model_validate(raw)
                except ValidationError:
                    return None
                return self._to_dto(schema)
        return None

    @classmethod
    def _to_dto(cls, schema: IxcEquipmentSchema) -> EquipmentDTO | None:
        product_name = schema.descricao or (
            f"Produto #{schema.id_produto}" if schema.id_produto else ""
        )
        return EquipmentDTO(
            external_id=schema.id,
            contract_external_id=schema.id_cliente_contrato or "",
            product_name=product_name,
            status=cls._map_status(schema.status),
            serial=schema.serial,
            mac=schema.mac,
            value=Decimal(schema.valor),
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _map_status(raw: str) -> str:
        return _STATUS_MAP.get((raw or "").upper().strip(), "UNKNOWN")
