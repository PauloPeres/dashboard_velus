"""Registro de ferramentas read-only expostas pelo servidor MCP.

Cada ferramenta é uma função fina sobre os `compute_*` do Analytics. A
organização NÃO é parâmetro: vem do contextvar setado pela camada de auth
(Bearer token → org). Assim o LLM nunca consegue pedir dados de outra org —
o isolamento multi-tenant é estrutural, não opcional.

Todas as ferramentas são de LEITURA. Nada aqui escreve no banco.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from apps.analytics.application import aggregations as agg
from apps.shared.context import get_current_organization
from apps.shared.exceptions import NoOrganizationInContextError


def _json_safe(value: Any) -> Any:
    """Converte Decimal/date/datetime para tipos serializáveis em JSON."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    raise TypeError(f"Tipo não serializável: {type(value).__name__}")


def _normalize(result: Any) -> Any:
    """Round-trip por JSON para garantir saída 100% serializável."""
    return json.loads(json.dumps(result, default=_json_safe))


def _require_org():
    org = get_current_organization()
    if org is None:
        raise NoOrganizationInContextError(
            "Nenhuma organização no contexto — token MCP ausente ou inválido."
        )
    return org


@dataclass(frozen=True)
class ToolSpec:
    """Descreve uma ferramenta MCP: nome, descrição e como executá-la."""

    name: str
    description: str
    handler: Callable[..., Any]
    months_param: bool = False
    limit_param: bool = False


def _wrap_org_only(fn: Callable[[Any], Any]) -> Callable[[], Any]:
    def handler() -> Any:
        return _normalize(fn(_require_org()))

    return handler


def _wrap_months(fn: Callable[..., Any], kw: str = "months") -> Callable[..., Any]:
    def handler(months: int = 12) -> Any:
        return _normalize(fn(_require_org(), **{kw: months}))

    return handler


def _wrap_limit(fn: Callable[..., Any]) -> Callable[..., Any]:
    def handler(limit: int = 20) -> Any:
        return _normalize(fn(_require_org(), limit=limit))

    return handler


# Catálogo de ferramentas — agrupado por domínio nas descrições.
TOOL_SPECS: list[ToolSpec] = [
    # ── Contratos / receita recorrente ──────────────────────────────────
    ToolSpec(
        "contratos_kpis",
        "KPIs atuais de contratos: MRR, contratos ativos, churn %, ticket "
        "médio (ARPU), inadimplência. Visão executiva do momento.",
        _wrap_org_only(agg.compute_kpis),
    ),
    ToolSpec(
        "contratos_mrr_series",
        "Série mensal de MRR (receita recorrente) nos últimos N meses.",
        _wrap_months(agg.compute_mrr_series),
        months_param=True,
    ),
    ToolSpec(
        "contratos_status_trend",
        "Evolução mensal de contratos por status (ativo, bloqueado, "
        "cancelado) nos últimos N meses.",
        _wrap_months(agg.compute_contract_status_trend),
        months_param=True,
    ),
    ToolSpec(
        "contratos_kpi_trend",
        "Evolução mensal de ARPU (ticket médio) e churn % por mês.",
        _wrap_months(agg.compute_contract_kpi_trend),
        months_param=True,
    ),
    ToolSpec(
        "contratos_churn_by_plan",
        "Churn por plano nos últimos N meses — quais planos mais cancelam.",
        _wrap_months(agg.compute_churn_by_plan),
        months_param=True,
    ),
    # ── Financeiro / caixa ──────────────────────────────────────────────
    ToolSpec(
        "financeiro_cash_received",
        "Recebimentos por mês (entrada de caixa real) nos últimos N meses.",
        _wrap_months(agg.compute_cash_received_series),
        months_param=True,
    ),
    ToolSpec(
        "financeiro_expenses",
        "Despesas pagas por mês nos últimos N meses.",
        _wrap_months(agg.compute_expense_series),
        months_param=True,
    ),
    ToolSpec(
        "financeiro_dre",
        "DRE gerencial simplificado (receita, custos, despesas, resultado) "
        "consolidando os últimos N meses.",
        _wrap_months(agg.compute_dre),
        months_param=True,
    ),
    ToolSpec(
        "financeiro_aging",
        "Distribuição de inadimplência por faixa de atraso (aging).",
        _wrap_org_only(agg.compute_aging_distribution),
    ),
    ToolSpec(
        "financeiro_delinquency_trend",
        "Inadimplência por mês de vencimento nos últimos N meses.",
        _wrap_months(agg.compute_delinquency_trend),
        months_param=True,
    ),
    ToolSpec(
        "financeiro_revenue_forecast",
        "Previsão de receita para os próximos N meses (tendência + sazonalidade).",
        _wrap_months(agg.compute_revenue_forecast, kw="months_ahead"),
        months_param=True,
    ),
    # ── Risco / clientes ────────────────────────────────────────────────
    ToolSpec(
        "risco_churn_summary",
        "Resumo de risco de churn: contagem por nível e receita em risco.",
        _wrap_org_only(agg.compute_churn_risk_summary),
    ),
    ToolSpec(
        "clientes_top_risco",
        "Top N clientes em maior risco de churn, ordenados por score.",
        _wrap_limit(agg.compute_top_risk_customers),
        limit_param=True,
    ),
    ToolSpec(
        "clientes_prioritarios",
        "Clientes a focar — prioriza por valor × risco e sugere a ação.",
        _wrap_limit(agg.compute_priority_customers),
        limit_param=True,
    ),
]
