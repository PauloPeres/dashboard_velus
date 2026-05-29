"""IxcInvoiceSource — implementação de InvoiceSourcePort para IXC.

Endpoint IXC: `fn` (financeiro_cliente). Status canônicos:
- A → PENDING (aberto)
- R → PAID (recebido)
- C → CANCELED
- AT → OVERDUE (em atraso) — IXC usa, mas pode-se derivar pelo vencimento
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.financial.domain.dto import InvoiceDTO
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.exceptions import AdapterContractError

from .client import IxcHttpClient
from .schemas import IxcInvoiceSchema

_logger = structlog.get_logger(__name__)


class IxcInvoiceSource:
    """Adapter IXC para a capability INVOICES."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.INVOICES})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_invoices(self, *, since: datetime | None = None) -> Iterator[InvoiceDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            for raw in client.paginate_ixc("fn_areceber", body_filter=body_filter):
                try:
                    schema = IxcInvoiceSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.error(
                        "ixc_invoice_schema_invalid",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:3],
                    )
                    raise AdapterContractError(
                        f"IXC retornou fatura com schema inválido (id={raw.get('id')!r}): {exc}"
                    ) from exc
                yield self._to_dto(schema)

    @staticmethod
    def _to_dto(schema: IxcInvoiceSchema) -> InvoiceDTO:
        status_map = {"A": "PENDING", "R": "PAID", "C": "CANCELED", "AT": "OVERDUE"}
        status = status_map.get(schema.status.upper(), "UNKNOWN")

        try:
            due_date = date.fromisoformat(schema.data_vencimento)
        except ValueError:
            # Sem data válida, default pra epoch — sync registra mas dashboard ignora
            due_date = date(1970, 1, 1)

        # IXC usa pagamento_data/pagamento_valor para data e valor efetivos do recebimento.
        # data_pgto (legado) geralmente vem null.
        paid_at = None
        if schema.pagamento_data:
            try:
                paid_at = datetime.fromisoformat(schema.pagamento_data)
            except ValueError:
                pass
        elif schema.data_pgto:
            paid_at = schema.data_pgto

        # Prefere pagamento_valor; fallback para valor_recebido ou valor_pago
        raw_paid = schema.pagamento_valor or schema.valor_recebido or schema.valor_pago
        paid_amount = Decimal(raw_paid) if raw_paid and raw_paid != "0" else None

        return InvoiceDTO(
            external_id=schema.id,
            contract_external_id=schema.id_contrato,
            amount=Decimal(schema.valor),
            due_date=due_date,
            status=status,
            issued_at=schema.data_emissao,
            paid_at=paid_at,
            paid_amount=paid_amount,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        return {
            "qtype": "fn_areceber.data_emissao",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
