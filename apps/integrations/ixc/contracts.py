"""IxcContractSource — implementação de ContractSourcePort para IXC."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

import structlog
from pydantic import ValidationError

from apps.customers.domain.dto import ContractDTO
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.exceptions import AdapterContractError

from .client import IxcHttpClient
from .plans import IxcPlanCache, PlanInfo
from .schemas import IxcContractSchema

_logger = structlog.get_logger(__name__)


class IxcAddonCache:
    """Cache lazy de add-ons (cliente_contrato_servicos). Agrupa por id_contrato."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._by_contract: dict[str, Decimal] = {}
        self._loaded = False

    def get_total(self, contract_id: str) -> Decimal:
        if not self._loaded:
            self._load()
        return self._by_contract.get(str(contract_id), Decimal("0"))

    def _load(self) -> None:
        from .schemas import IxcContractServiceSchema

        try:
            for raw in self._client.paginate_ixc("cliente_contrato_servicos", page_size=200):
                try:
                    schema = IxcContractServiceSchema.model_validate(raw)
                except ValidationError:
                    continue
                # Only count active services (status != CA)
                if schema.status.upper() == "CA":
                    continue
                amount = Decimal(schema.valor_total)
                cid = schema.id_contrato
                self._by_contract[cid] = self._by_contract.get(cid, Decimal("0")) + amount
        except Exception as exc:
            _logger.warning("ixc_addon_cache_failed", error=str(exc))
        self._loaded = True
        _logger.info("ixc_addon_cache_loaded", contracts_with_addons=len(self._by_contract))


class IxcDiscountSurchargeCache:
    """Cache lazy de descontos + acréscimos. Agrupa net por id_contrato.

    Net = sum(acréscimos) - sum(descontos). Positive = net surcharge, negative = net discount.
    We store discounts as positive (amount to subtract from MRR).
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._discounts: dict[str, Decimal] = {}
        self._loaded = False

    def get_total_discounts(self, contract_id: str) -> Decimal:
        """Returns total discounts (positive value to subtract from MRR)."""
        if not self._loaded:
            self._load()
        return self._discounts.get(str(contract_id), Decimal("0"))

    def _load(self) -> None:
        from datetime import date

        from .schemas import IxcContractDiscountSchema, IxcContractSurchargeSchema

        today = date.today().isoformat()

        # Load discounts
        try:
            for raw in self._client.paginate_ixc("cliente_contrato_descontos", page_size=200):
                try:
                    schema = IxcContractDiscountSchema.model_validate(raw)
                except ValidationError:
                    continue
                # Skip expired discounts
                if schema.data_validade and schema.data_validade < today:
                    continue
                amount = Decimal(schema.valor)
                cid = schema.id_contrato
                self._discounts[cid] = self._discounts.get(cid, Decimal("0")) + abs(amount)
        except Exception as exc:
            _logger.warning("ixc_discount_cache_failed", error=str(exc))

        # Load surcharges (subtract from discounts — they ADD to revenue)
        try:
            for raw in self._client.paginate_ixc("cliente_contrato_acrescimos", page_size=200):
                try:
                    schema = IxcContractSurchargeSchema.model_validate(raw)
                except ValidationError:
                    continue
                if schema.data_validade and schema.data_validade < today:
                    continue
                amount = Decimal(schema.valor)
                cid = schema.id_contrato
                # Surcharges reduce the discount total (they're revenue additions)
                self._discounts[cid] = self._discounts.get(cid, Decimal("0")) - abs(amount)
        except Exception as exc:
            _logger.warning("ixc_surcharge_cache_failed", error=str(exc))

        self._loaded = True
        _logger.info("ixc_discount_cache_loaded", contracts_with_adjustments=len(self._discounts))


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
            addon_cache = IxcAddonCache(client)
            discount_cache = IxcDiscountSurchargeCache(client)
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
                yield self._to_dto(schema, plan_cache, addon_cache, discount_cache)

    def get_contract(self, external_id: str) -> ContractDTO | None:
        body_filter = {"qtype": "cliente_contrato.id", "query": str(external_id), "oper": "="}
        with self._client_factory() as client:
            plan_cache = IxcPlanCache(client)
            addon_cache = IxcAddonCache(client)
            discount_cache = IxcDiscountSurchargeCache(client)
            for raw in client.paginate_ixc("cliente_contrato", body_filter=body_filter, page_size=1):
                try:
                    schema = IxcContractSchema.model_validate(raw)
                except ValidationError as exc:
                    raise AdapterContractError(
                        f"IXC schema inválido id={external_id}: {exc}"
                    ) from exc
                return self._to_dto(schema, plan_cache, addon_cache, discount_cache)
        return None

    @staticmethod
    def _to_dto(
        schema: IxcContractSchema,
        plan_cache: IxcPlanCache,
        addon_cache: IxcAddonCache,
        discount_cache: IxcDiscountSurchargeCache,
    ) -> ContractDTO:
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

        # Hardening: IXC às vezes retorna status=null (→ ACTIVE/UNKNOWN) em contratos
        # que já foram cancelados. Se motivo_cancelamento está definido e não-zero,
        # o contrato é definitivamente cancelado — override independente do campo status.
        motivo_cancelamento = str(extras.get("motivo_cancelamento") or "0")
        if motivo_cancelamento not in ("0", "", "null") and status not in ("CANCELED",):
            status = "CANCELED"
            _logger.debug(
                "ixc_contract_status_override",
                external_id=schema.id,
                original_status=schema.status,
                motivo_cancelamento=motivo_cancelamento,
            )

        monthly_amount_addons = addon_cache.get_total(schema.id)
        monthly_amount_discounts = discount_cache.get_total_discounts(schema.id)

        return ContractDTO(
            external_id=schema.id,
            customer_external_id=schema.id_cliente,
            plan_name=plan_name,
            monthly_amount=monthly_amount,
            monthly_amount_addons=monthly_amount_addons,
            monthly_amount_discounts=max(monthly_amount_discounts, Decimal("0")),  # clamp to 0 if surcharges > discounts
            status=status,
            activated_at=schema.data_ativacao,
            canceled_at=canceled_at,
            address=schema.endereco,
            raw_extras=extras,
        )

