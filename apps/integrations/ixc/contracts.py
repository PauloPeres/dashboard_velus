"""IxcContractSource — implementação de ContractSourcePort para IXC."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.customers.domain.dto import ContractDTO
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.exceptions import AdapterContractError

from .client import IxcHttpClient
from .schemas import IxcContractSchema

_logger = structlog.get_logger(__name__)


class IxcContractSource:
    """Adapter IXC para a capability CONTRACTS."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CONTRACTS})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_contracts(self, *, since: datetime | None = None) -> Iterator[ContractDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            for raw in client.paginate_ixc("contrato", body_filter=body_filter):
                try:
                    schema = IxcContractSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.error(
                        "ixc_contract_schema_invalid",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:3],
                    )
                    raise AdapterContractError(
                        f"IXC retornou contrato com schema inválido (id={raw.get('id')!r}): {exc}"
                    ) from exc
                yield self._to_dto(schema)

    def get_contract(self, external_id: str) -> ContractDTO | None:
        body_filter = {"qtype": "contrato.id", "query": str(external_id), "oper": "="}
        with self._client_factory() as client:
            for raw in client.paginate_ixc("contrato", body_filter=body_filter, page_size=1):
                try:
                    schema = IxcContractSchema.model_validate(raw)
                except ValidationError as exc:
                    raise AdapterContractError(
                        f"IXC schema inválido id={external_id}: {exc}"
                    ) from exc
                return self._to_dto(schema)
        return None

    @staticmethod
    def _to_dto(schema: IxcContractSchema) -> ContractDTO:
        status_map = {
            "A": "ACTIVE", "B": "BLOCKED", "CA": "CANCELED",
            "AA": "AWAITING_INSTALL", "FI": "ACTIVE",  # FI = Financeiro em atraso (ainda ativo)
        }
        # status_internet refina o status do contrato — se contrato é ativo (A)
        # mas internet está bloqueada, marca como BLOCKED
        if schema.status == "A" and schema.status_internet in ("CM", "FA"):
            status = "BLOCKED"
        else:
            status = status_map.get(schema.status.upper(), "UNKNOWN")

        plan_name = schema.descricao_plano or schema.id_vd_contrato or "—"

        return ContractDTO(
            external_id=schema.id,
            customer_external_id=schema.id_cliente,
            plan_name=plan_name,
            monthly_amount=Decimal(schema.mensalidade),
            status=status,
            activated_at=schema.data_ativacao,
            canceled_at=schema.data_cancelamento,
            address=schema.endereco,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "qtype": "contrato.data_alteracao",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
