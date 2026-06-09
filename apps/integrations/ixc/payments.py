"""IxcPaymentSource — implementação de PaymentSourcePort para IXC.

Endpoint IXC: `fn_areceber_baixas` (baixas de recebíveis). Cada baixa é um
recebimento efetivo atrelado a uma fatura (`id_areceber`). Diferente do
campo pagamento_data da fatura, as baixas capturam pagamentos parciais e
múltiplas liquidações — base para análise de recuperação de inadimplência.

Incremental por `data_baixa >= since`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from typing import ClassVar

import structlog
from pydantic import ValidationError

from apps.financial.domain.dto import PaymentDTO
from apps.integrations.shared.enums import Capability, SourceType

from .client import IxcHttpClient
from .schemas import IxcPaymentSchema

_logger = structlog.get_logger(__name__)

# Mapeia a forma de pagamento do IXC (texto livre/código) pro método canônico
# do PaymentDTO. Match por substring case-insensitive. Fallback UNKNOWN — o
# valor cru fica em raw_extras pra auditoria.
_METHOD_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("pix", "PIX"),
    ("boleto", "BOLETO"),
    ("cartao", "CARD"),
    ("cartão", "CARD"),
    ("cart", "CARD"),
    ("credito", "CARD"),
    ("debito", "CARD"),
    ("transfer", "TRANSFER"),
    ("ted", "TRANSFER"),
    ("doc", "TRANSFER"),
    ("dinheiro", "CASH"),
    ("especie", "CASH"),
    ("espécie", "CASH"),
)


class IxcPaymentSource:
    """Adapter IXC para a capability PAYMENTS."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.PAYMENTS})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_payments(self, *, since: datetime | None = None) -> Iterator[PaymentDTO]:
        body_filter = self._build_since_filter(since) if since else None

        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_ixc("fn_areceber_baixas", body_filter=body_filter):
                try:
                    schema = IxcPaymentSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_baixa_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue

                dto = self._to_dto(schema)
                if dto is not None:
                    yield dto

            if skipped:
                _logger.info("ixc_payment_list_done", skipped=skipped)

    @classmethod
    def _to_dto(cls, schema: IxcPaymentSchema) -> PaymentDTO | None:
        # Baixa sem data não é um recebimento utilizável — PaymentDTO exige paid_at.
        if schema.data_baixa is None:
            return None

        extras = schema.get_extras()
        extras.update(
            {
                "juros": schema.juros,
                "multa": schema.multa,
                "desconto": schema.desconto,
                "forma_pagamento": schema.forma_pagamento,
            }
        )

        return PaymentDTO(
            external_id=schema.id,
            invoice_external_id=schema.id_areceber or None,
            contract_external_id=None,
            amount=Decimal(schema.valor),
            paid_at=schema.data_baixa,
            method=cls._map_method(schema.forma_pagamento, schema.historico),
            raw_extras=extras,
        )

    @staticmethod
    def _map_method(forma: str, historico: str = "") -> str:
        # tipo_recebimento (forma) é só código contábil; o método legível está
        # no histórico ("[Empresa - Pix - Banco]"). Varre os dois por keyword.
        text = f"{forma or ''} {historico or ''}".lower().strip()
        if not text:
            return "UNKNOWN"
        for keyword, canonical in _METHOD_KEYWORDS:
            if keyword in text:
                return canonical
        return "UNKNOWN"

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        from zoneinfo import ZoneInfo
        sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
        # qtype precisa ser o nome cru da coluna (`data`); o IXC passou a
        # rejeitar o prefixo de tabela (`fn_areceber_baixas.data`) nesse recurso
        # de função, devolvendo página HTML de erro e derrubando o sync.
        return {
            "qtype": "data",
            "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
            "oper": ">=",
        }
