"""IxcExpenseSource — adapter para contas a pagar (fn_apagar) do IXC.

Inclui IxcSupplierCache (lazy load de fornecedores) e inferência de categoria
por keyword matching no campo obs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar

import structlog
from pydantic import ValidationError

from apps.financial.domain.dto import ExpenseDTO
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.exceptions import AdapterContractError

from .schemas import IxcExpenseSchema, IxcSupplierSchema

_logger = structlog.get_logger(__name__)

# IXC status codes → domain status
_STATUS_MAP = {"F": "PAID", "A": "OPEN", "C": "CANCELED"}

# Keyword → category rules (uppercase matching)
_CATEGORY_RULES: list[tuple[list[str], str]] = [
    (["LINK", "UPLINK", "TRÂNSITO", "TRANSITO", "FIBRA", "PONTO"], "Conectividade"),
    (["SALÁRIO", "SALARIO", "PROLAB", "PRÓ-LABOR", "PRO-LABOR", "FOLHA", "FUNCIONÁR", "FUNCIONAR"], "Pessoal"),
    (["ALUGUEL", "CONDOMÍN", "CONDOMI"], "Aluguel"),
    (["ENERGIA", "ELÉTRIC", "ELETRIC", "ÁGUA", "AGUA"], "Utilidades"),
    (["IMPOSTO", "DAS ", "SIMPLES", "ISS", "INSS", "FGTS"], "Impostos"),
    (["EQUIPAMENTO", "ROTEADOR", "ONU", "SWITCH"], "Equipamentos"),
]


def _infer_category(text: str) -> str:
    """Infere categoria de uma despesa pelo texto descritivo."""
    upper = text.upper()
    for keywords, category in _CATEGORY_RULES:
        if any(kw in upper for kw in keywords):
            return category
    return ""


def _parse_date(value: str) -> date | None:
    """Converte string YYYY-MM-DD em date; retorna None se inválido."""
    if not value or value in ("0000-00-00",):
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def _parse_decimal(value: str) -> Decimal:
    """Converte string monetária em Decimal; retorna 0 se inválido."""
    try:
        cleaned = re.sub(r"[^\d.,\-]", "", value).replace(",", ".")
        return Decimal(cleaned) if cleaned else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


class IxcSupplierCache:
    """Cache lazy de Fornecedores. 1 GET no primeiro acesso; subsequentes in-memory.

    Segue o mesmo padrão de IxcPlanCache — carrega endpoint `fornecedor` uma
    vez por sync e mapeia id → nome fantasia.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._suppliers: dict[str, str] = {}  # id → nome fantasia
        self._loaded = False

    def get_name(self, supplier_id: str) -> str:
        if not self._loaded:
            self._load()
        return self._suppliers.get(str(supplier_id), "")

    def _load(self) -> None:
        try:
            for raw in self._client.paginate_ixc("fornecedor", page_size=200):
                try:
                    schema = IxcSupplierSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "ixc_supplier_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                name = schema.display_name
                self._suppliers[schema.id] = name
        except Exception as exc:
            raise AdapterContractError(
                f"Falha ao carregar cache de fornecedores IXC: {type(exc).__name__}: {exc}"
            ) from exc
        self._loaded = True
        _logger.info("ixc_supplier_cache_loaded", count=len(self._suppliers))


class IxcExpenseSource:
    """Adapter que lê contas a pagar (fn_apagar) da API IXC.

    Pagina o endpoint `fn_apagar`, valida via IxcExpenseSchema, enriquece com
    nome do fornecedor via IxcSupplierCache e infere categoria por keyword.

    Segue o mesmo padrão de IxcInvoiceSource/IxcContractSource:
    `__init__` guarda uma factory; `list_expenses` abre o client como context manager.
    """

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.EXPENSES})

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        from .client import IxcHttpClient

        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_expenses(
        self, *, since: datetime | None = None
    ) -> Iterator[ExpenseDTO]:
        """Itera despesas da API IXC.

        since=None → bootstrap (todos os registros).
        since=datetime → filtra por data_emissao >= since (formato YYYY-MM-DD HH:MM:SS).
        """
        body_filter = None
        if since is not None:
            from zoneinfo import ZoneInfo
            sp = since.astimezone(ZoneInfo("America/Sao_Paulo"))
            body_filter = {
                "qtype": "fn_apagar.data_emissao",
                "query": sp.strftime("%Y-%m-%d %H:%M:%S"),
                "oper": ">=",
            }

        with self._client_factory() as client:
            supplier_cache = IxcSupplierCache(client)
            skipped = 0
            for raw in client.paginate_ixc("fn_apagar", body_filter=body_filter):
                try:
                    schema = IxcExpenseSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "ixc_expense_schema_invalid_skipped",
                        external_id=raw.get("id"),
                        errors=exc.errors()[:1],
                    )
                    continue

                dto = self._to_dto(schema, supplier_cache)
                if dto is not None:
                    yield dto

            if skipped:
                _logger.info("ixc_expense_list_done", skipped=skipped)

    @staticmethod
    def _to_dto(schema: IxcExpenseSchema, supplier_cache: IxcSupplierCache) -> ExpenseDTO | None:
        due = _parse_date(schema.data_vencimento)
        if due is None:
            _logger.warning(
                "ixc_expense_no_due_date_skipped", external_id=schema.id
            )
            return None

        supplier_name = (
            supplier_cache.get_name(schema.id_fornecedor)
            if schema.id_fornecedor
            else ""
        )

        status = _STATUS_MAP.get(schema.status.upper(), "OPEN")
        amount = _parse_decimal(schema.valor)
        valor_pago_str = schema.valor_pago or "0"
        paid_amount = _parse_decimal(valor_pago_str) if valor_pago_str != "0" else None

        description = schema.obs or ""
        category = _infer_category(description or supplier_name)

        return ExpenseDTO(
            external_id=schema.id,
            supplier_external_id=schema.id_fornecedor or "",
            supplier_name=supplier_name,
            description=description,
            amount=amount,
            due_date=due,
            status=status,
            payment_type=schema.tipo_pagamento,
            category=category,
            issued_at=_parse_date(schema.data_emissao),
            paid_at=_parse_date(schema.data_pagamento),
            paid_amount=paid_amount,
            raw_extras=schema.get_extras(),
        )
