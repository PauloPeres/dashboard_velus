"""IxcOpportunitySource — implementação de OpportunitySourcePort para IXC.

Endpoint IXC: `crm_negociacoes` (negociações/oportunidades do CRM). Cada
registro é uma negociação atrelada a um lead (`id_candidato`), com valor e
estado (em andamento, ganha, perdida).

Status IXC mapeado pro status canônico do OpportunityDTO; valores
desconhecidos -> UNKNOWN (valor cru preservado em raw_extras).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.sales.domain.dto import OpportunityDTO

from .client import IxcHttpClient
from .schemas import IxcOpportunitySchema

_logger = structlog.get_logger(__name__)

# Mapeia o status da negociação IXC pro status canônico do OpportunityDTO.
_STATUS_MAP: dict[str, str] = {
    "A": "OPEN",
    "ABERTO": "OPEN",
    "EM_ANDAMENTO": "OPEN",
    "ANDAMENTO": "OPEN",
    "G": "WON",
    "GANHO": "WON",
    "GANHA": "WON",
    "P": "LOST",
    "PERDIDO": "LOST",
    "PERDIDA": "LOST",
}


class IxcOpportunitySource:
    """Adapter IXC para a capability OPPORTUNITIES."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OPPORTUNITIES}
    )

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_opportunities(
        self, *, since: datetime | None = None
    ) -> Iterator[OpportunityDTO]:
        # CRM IXC não expõe filtro de data confiável — full scan. O upsert
        # idempotente cuida da reconciliação.
        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_ixc("crm_negociacoes"):
                try:
                    schema = IxcOpportunitySchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_opportunity_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield self._to_dto(schema)

            if skipped:
                _logger.info("ixc_opportunity_list_done", skipped=skipped)

    def get_opportunity(self, external_id: str) -> OpportunityDTO | None:
        with self._client_factory() as client:
            body_filter = {
                "qtype": "crm_negociacoes.id",
                "query": external_id,
                "oper": "=",
            }
            for raw in client.paginate_ixc(
                "crm_negociacoes", body_filter=body_filter
            ):
                try:
                    schema = IxcOpportunitySchema.model_validate(raw)
                except ValidationError:
                    return None
                return self._to_dto(schema)
        return None

    @classmethod
    def _to_dto(cls, schema: IxcOpportunitySchema) -> OpportunityDTO:
        return OpportunityDTO(
            external_id=schema.id,
            lead_external_id=schema.id_candidato or "",
            status=cls._map_status(schema.status),
            value=Decimal(schema.valor),
            loss_reason=schema.motivo_perda,
            created_at_source=schema.data_criacao,
            closed_at=schema.data_fechamento,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _map_status(raw: str) -> str:
        return _STATUS_MAP.get((raw or "").upper().strip(), "UNKNOWN")
