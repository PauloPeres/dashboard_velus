"""IxcLeadSource — implementação de LeadSourcePort para IXC.

Endpoint IXC: `crm_canditados` (leads/prospects do CRM). Cada registro é um
candidato no funil de vendas, com estágio de prospecção e canal de origem.

Status IXC (`status_prospeccao`) mapeado pro status canônico do LeadDTO;
valores desconhecidos -> UNKNOWN (valor cru preservado em raw_extras).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.sales.domain.dto import LeadDTO

from .client import IxcHttpClient
from .schemas import IxcLeadSchema

_logger = structlog.get_logger(__name__)

# Mapeia o estágio de prospecção IXC pro status canônico do LeadDTO.
_STATUS_MAP: dict[str, str] = {
    "N": "NEW",
    "NOVO": "NEW",
    "C": "CONTACTED",
    "EM_CONTATO": "CONTACTED",
    "CONTATO": "CONTACTED",
    "G": "CONVERTED",
    "GANHO": "CONVERTED",
    "CONVERTIDO": "CONVERTED",
    "P": "LOST",
    "PERDIDO": "LOST",
}


class IxcLeadSource:
    """Adapter IXC para a capability LEADS."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.LEADS})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_leads(self, *, since: datetime | None = None) -> Iterator[LeadDTO]:
        # CRM IXC não expõe filtro de data confiável — full scan. O upsert
        # idempotente cuida da reconciliação.
        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_ixc("crm_canditados"):
                try:
                    schema = IxcLeadSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_lead_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield self._to_dto(schema)

            if skipped:
                _logger.info("ixc_lead_list_done", skipped=skipped)

    def get_lead(self, external_id: str) -> LeadDTO | None:
        with self._client_factory() as client:
            body_filter = {
                "qtype": "crm_canditados.id",
                "query": external_id,
                "oper": "=",
            }
            for raw in client.paginate_ixc(
                "crm_canditados", body_filter=body_filter
            ):
                try:
                    schema = IxcLeadSchema.model_validate(raw)
                except ValidationError:
                    return None
                return self._to_dto(schema)
        return None

    @classmethod
    def _to_dto(cls, schema: IxcLeadSchema) -> LeadDTO:
        return LeadDTO(
            external_id=schema.id,
            name=schema.nome,
            status=cls._map_status(schema.status_prospeccao),
            phone=schema.telefone,
            email=schema.email,
            origin=schema.origem,
            salesperson_id=schema.id_vendedor,
            created_at_source=schema.data_cadastro,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _map_status(raw: str) -> str:
        return _STATUS_MAP.get((raw or "").upper().strip(), "UNKNOWN")
