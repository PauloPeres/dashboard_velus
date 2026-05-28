"""IxcCustomerSource — implementação do CustomerSourcePort para IXC Soft.

Responsabilidades:
1. Paginar `cliente` via IxcHttpClient (formato IXC: GET com body, `page` + `rp`).
2. Validar cada registro com Pydantic IxcCustomerSchema (Anti-Corruption Layer).
3. Traduzir schema validado → CustomerDTO neutro.

Filtros usados:
- Bootstrap (since=None): sem filtro além de paginação.
- Incremental (since=<dt>): filtro `data_alteracao >= since` se IXC suportar
  no payload. Confirmar com instalação real — fallback é puxar tudo e filtrar
  client-side (mais lento mas robusto).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.customers.domain.dto import CustomerDTO
from apps.customers.domain.services import normalize_document
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.exceptions import AdapterContractError

from .client import IxcHttpClient
from .schemas import IxcCustomerSchema

_logger = structlog.get_logger(__name__)


class IxcCustomerSource:
    """Adapter IXC para a capability CUSTOMERS — implementa `CustomerSourcePort`."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CUSTOMERS})

    def __init__(
        self,
        *,
        base_url: str,
        user_id: str,
        api_token: str,
    ) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url,
            user_id=user_id,
            api_token=api_token,
        )

    # -------------------------------------------------------------------------
    # CustomerSourcePort
    # -------------------------------------------------------------------------
    def list_customers(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[CustomerDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            for raw in client.paginate_ixc("cliente", body_filter=body_filter):
                try:
                    schema = IxcCustomerSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.error(
                        "ixc_customer_schema_invalid",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:3],
                    )
                    raise AdapterContractError(
                        f"IXC retornou cliente com schema inválido (id={raw.get('id')!r}): {exc}"
                    ) from exc

                yield self._to_dto(schema)

    def get_customer(self, external_id: str) -> CustomerDTO | None:
        """Busca cliente único — usa filtro `cliente.id = external_id`."""
        body_filter = {
            "qtype": "cliente.id",
            "query": str(external_id),
            "oper": "=",
        }
        with self._client_factory() as client:
            for raw in client.paginate_ixc("cliente", body_filter=body_filter, page_size=1):
                try:
                    schema = IxcCustomerSchema.model_validate(raw)
                except ValidationError as exc:
                    raise AdapterContractError(
                        f"IXC retornou schema inválido para id={external_id}: {exc}"
                    ) from exc
                return self._to_dto(schema)
        return None

    # -------------------------------------------------------------------------
    # Anti-Corruption Layer — schema validado → DTO neutro
    # -------------------------------------------------------------------------
    @staticmethod
    def _to_dto(schema: IxcCustomerSchema) -> CustomerDTO:
        if schema.is_active:
            status = "ACTIVE"
        elif schema.ativo.upper() == "N":
            status = "CANCELED"
        else:
            status = "UNKNOWN"

        return CustomerDTO(
            external_id=schema.id,
            document=normalize_document(schema.cnpj_cpf),
            name=schema.razao,
            email=schema.email,
            phone=schema.telefone_celular,
            status=status,
            created_at_source=schema.data_cadastro,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        """Constrói filtro de incremental baseado em `data_alteracao`.

        IXC usa formato `YYYY-MM-DD HH:MM:SS` em queries. Convertemos pra
        horário local de São Paulo (IXC roda local).
        """
        from zoneinfo import ZoneInfo

        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "qtype": "cliente.data_alteracao",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
