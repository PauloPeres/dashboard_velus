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
from .plans import IxcPlanCache, PlanInfo
from .schemas import IxcContractSchema

_logger = structlog.get_logger(__name__)


class IxcContractSource:
    """Adapter IXC para a capability CONTRACTS.

    Pra resolver nome e valor mensal do plano (cliente_contrato só tem FK
    `id_vd_contrato`), usa IxcPlanCache: 1 GET em vd_contratos no primeiro
    acesso, depois lookup in-memory por contrato.
    """

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CONTRACTS})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_contracts(self, *, since: datetime | None = None) -> Iterator[ContractDTO]:
        # IXC não expõe `data_alteracao` no endpoint `cliente_contrato` — filtro
        # server-side retorna HTML de erro (HTTP 200). Full scan garante que mudanças
        # de status (ACTIVE→CANCELED) sejam capturadas; SCD2 no repo cuida da idempotência.
        if since:
            _logger.debug(
                "ixc_contract_incremental_full_scan",
                since=since.isoformat(),
                reason="data_alteracao filter unsupported on cliente_contrato endpoint",
            )

        with self._client_factory() as client:
            plan_cache = IxcPlanCache(client)
            for raw in client.paginate_ixc("cliente_contrato"):
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
                yield self._to_dto(schema, plan_cache)

    def get_contract(self, external_id: str) -> ContractDTO | None:
        body_filter = {"qtype": "cliente_contrato.id", "query": str(external_id), "oper": "="}
        with self._client_factory() as client:
            plan_cache = IxcPlanCache(client)
            for raw in client.paginate_ixc("cliente_contrato", body_filter=body_filter, page_size=1):
                try:
                    schema = IxcContractSchema.model_validate(raw)
                except ValidationError as exc:
                    raise AdapterContractError(
                        f"IXC schema inválido id={external_id}: {exc}"
                    ) from exc
                return self._to_dto(schema, plan_cache)
        return None

    @staticmethod
    def _to_dto(schema: IxcContractSchema, plan_cache: IxcPlanCache) -> ContractDTO:
        status_map = {
            "A": "ACTIVE", "B": "BLOCKED", "CA": "CANCELED",
            "AA": "AWAITING_INSTALL", "FI": "ACTIVE",  # FI = Financeiro em atraso (ainda ativo)
        }
        # status_internet refina o status do contrato — se contrato é ativo (A)
        # mas internet está bloqueada, marca como BLOCKED
        if schema.status == "A" and schema.status_internet in ("CM", "FA"):
            status = "BLOCKED"
        elif not schema.status:
            # IXC retorna status=null para contratos cancelados/abandonados.
            status = "CANCELED"
        else:
            # Códigos conhecidos mapeados; qualquer outro código (ex: "D" desistência,
            # "I" inativo) é tratado como CANCELED — nunca são assinantes ativos.
            status = status_map.get(schema.status.upper(), "CANCELED")

        # Lookup do plano pra obter nome e valor mensal
        plan: PlanInfo | None = None
        if schema.id_vd_contrato:
            plan = plan_cache.get(schema.id_vd_contrato)

        plan_name = (
            plan.name if plan else (
                schema.descricao_plano or f"Plano #{schema.id_vd_contrato}" or "—"
            )
        )
        monthly_amount = plan.monthly_amount if plan else Decimal(schema.mensalidade)

        # canceled_at: usa data_cancelamento se disponível, senão data_desistencia
        # (pré-contratos abandonados têm data_desistencia mas não data_cancelamento)
        extras = schema.get_extras()
        canceled_at = schema.data_cancelamento
        if canceled_at is None and status == "CANCELED":
            raw_desistencia = extras.get("data_desistencia")
            if raw_desistencia and raw_desistencia not in ("0000-00-00", "", None):
                try:
                    from datetime import datetime as _dt
                    from django.utils.timezone import make_aware
                    canceled_at = make_aware(_dt.strptime(raw_desistencia, "%Y-%m-%d"))
                except (ValueError, TypeError):
                    pass

        return ContractDTO(
            external_id=schema.id,
            customer_external_id=schema.id_cliente,
            plan_name=plan_name,
            monthly_amount=monthly_amount,
            status=status,
            activated_at=schema.data_ativacao,
            canceled_at=canceled_at,
            address=schema.endereco,
            raw_extras=extras,
        )

