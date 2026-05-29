"""Cache de Plans (vd_contratos) — usado por IxcContractSource pra enriquecer.

cliente_contrato no IXC só carrega `id_vd_contrato` (FK); nome e valor mensal
moram em vd_contratos. Pra evitar 1 GET por contrato (muito devagar), carrega
todos os planos uma vez por sync e cacheia em dict.

Volumes típicos: ISPs pequenos têm 10-100 planos. Carga inicial cabe em 1
página de paginação (page_size=200 default).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from pydantic import ValidationError

from apps.integrations.shared.exceptions import AdapterContractError

from .schemas import IxcPlanSchema

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PlanInfo:
    """Dados normalizados de um Plan, lookup-friendly."""
    name: str
    monthly_amount: Decimal


class IxcPlanCache:
    """Cache lazy de Plans. 1 GET no primeiro acesso; subsequentes são in-memory."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self._plans: dict[str, PlanInfo] = {}
        self._loaded = False

    def get(self, plan_id: str) -> PlanInfo | None:
        if not self._loaded:
            self._load()
        return self._plans.get(str(plan_id))

    def _load(self) -> None:
        try:
            for raw in self._client.paginate_ixc("vd_contratos", page_size=200):
                try:
                    schema = IxcPlanSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "ixc_plan_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                self._plans[schema.id] = PlanInfo(
                    name=schema.nome or schema.descricao or f"Plano #{schema.id}",
                    monthly_amount=Decimal(schema.valor_contrato),
                )
        except Exception as exc:
            raise AdapterContractError(
                f"Falha ao carregar cache de planos IXC: {type(exc).__name__}: {exc}"
            ) from exc

        self._loaded = True
        _logger.info("ixc_plan_cache_loaded", count=len(self._plans))
