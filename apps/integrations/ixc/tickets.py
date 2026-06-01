"""IxcTicketSource — implementacao de TicketSourcePort para IXC.

Endpoint IXC: `su_oss_chamado`. Status canonicos:
- AG -> SCHEDULED (agendado)
- A  -> OPEN (aberto)
- EX -> IN_PROGRESS (em execucao)
- F  -> CLOSED (fechado/finalizado)
- EN -> FORWARDED (encaminhado)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.helpdesk.domain.dto import TicketDTO
from apps.integrations.shared.enums import Capability, SourceType

from .client import IxcHttpClient
from .schemas import IxcTicketSchema

_logger = structlog.get_logger(__name__)

# IXC status codes -> domain status
_STATUS_MAP = {
    "AG": "SCHEDULED",
    "A": "OPEN",
    "EX": "IN_PROGRESS",
    "F": "CLOSED",
    "EN": "FORWARDED",
}

# IXC priority codes -> domain priority
_PRIORITY_MAP = {
    "N": "NORMAL",
    "A": "HIGH",
    "B": "LOW",
    "U": "URGENT",
}


class IxcTicketSource:
    """Adapter IXC para a capability TICKETS."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.TICKETS})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_tickets(self, *, since: datetime | None = None) -> Iterator[TicketDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_ixc("su_oss_chamado", body_filter=body_filter):
                try:
                    schema = IxcTicketSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_ticket_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue

                dto = self._to_dto(schema)
                if dto is not None:
                    yield dto

            if skipped:
                _logger.info("ixc_ticket_list_done", skipped=skipped)

    def get_ticket(self, external_id: str) -> TicketDTO | None:
        with self._client_factory() as client:
            body_filter = {
                "qtype": "su_oss_chamado.id",
                "query": external_id,
                "oper": "=",
            }
            for raw in client.paginate_ixc("su_oss_chamado", body_filter=body_filter):
                try:
                    schema = IxcTicketSchema.model_validate(raw)
                except ValidationError:
                    return None
                return self._to_dto(schema)
        return None

    @staticmethod
    def _to_dto(schema: IxcTicketSchema) -> TicketDTO | None:
        status = _STATUS_MAP.get(schema.status.upper(), "OPEN")
        priority = _PRIORITY_MAP.get(schema.prioridade.upper(), "NORMAL")

        return TicketDTO(
            external_id=schema.id,
            customer_external_id=schema.id_cliente,
            subject_id=schema.id_assunto,
            sector=schema.setor,
            technician_id=schema.id_tecnico,
            status=status,
            priority=priority,
            message=schema.mensagem or "",
            protocol=schema.protocolo,
            opened_at=schema.data_abertura,
            scheduled_at=schema.data_agenda,
            closed_at=schema.data_fechamento,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "qtype": "su_oss_chamado.ultima_atualizacao",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
