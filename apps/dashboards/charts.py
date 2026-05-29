"""Helpers Plotly — gera JSON pronto pra embed em template.

Cada função recebe dados já agregados e retorna `(div_id, json_str)` que o
template renderiza via `Plotly.newPlot(div_id, JSON.parse(json_str), {...})`.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

_LAYOUT_BASE: dict[str, Any] = {
    "margin": {"l": 50, "r": 20, "t": 30, "b": 50},
    "font": {"family": "system-ui, sans-serif", "size": 12},
    "plot_bgcolor": "#fafafa",
    "paper_bgcolor": "#ffffff",
    "showlegend": False,
}


def _to_json(fig: go.Figure) -> str:
    return fig.to_json()


def mrr_line_chart(series: list[dict[str, Any]]) -> str:
    """Gráfico de linha — MRR mês a mês."""
    labels = [p["label"] for p in series]
    values = [p["mrr"] for p in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values, mode="lines+markers",
                line={"color": "#2563eb", "width": 3},
                marker={"size": 8, "color": "#2563eb"},
                hovertemplate="<b>%{x}</b><br>R$ %{y:,.2f}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"}},
    )
    return _to_json(fig)


def aging_bar_chart(buckets: list[dict[str, Any]]) -> str:
    """Gráfico de barras — distribuição de inadimplência por aging bucket."""
    labels = [b["label"] for b in buckets]
    values = [b["amount"] for b in buckets]
    colors = ["#10b981", "#fbbf24", "#f97316", "#ef4444", "#991b1b"]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=values,
                marker_color=colors[: len(labels)],
                hovertemplate="<b>%{x}</b><br>R$ %{y:,.2f}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"}},
    )
    return _to_json(fig)


def arpu_bar_chart(arpu_data: list[dict[str, Any]]) -> str:
    """Barras horizontais — receita por plano."""
    labels = [p["plan"] for p in arpu_data]
    values = [p["revenue"] for p in arpu_data]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#0ea5e9",
                hovertemplate="<b>%{y}</b><br>R$ %{x:,.2f}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"}},
    )
    return _to_json(fig)


def pipeline_pie(pipeline: list[dict[str, Any]]) -> str:
    """Pizza — distribuição de contratos por status."""
    fig = go.Figure(
        data=[
            go.Pie(
                labels=[p["status"] for p in pipeline],
                values=[p["count"] for p in pipeline],
                hole=0.4,
                marker={"colors": ["#10b981", "#fbbf24", "#ef4444", "#6b7280"]},
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def cash_received_chart(series: list[dict[str, Any]]) -> str:
    """Linha — recebimentos mensais."""
    labels = [p["label"] for p in series]
    values = [p["amount"] for p in series]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=values,
                marker_color="#16a34a",
                hovertemplate="<b>%{x}</b><br>R$ %{y:,.2f}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"}},
    )
    return _to_json(fig)


def cashflow_waterfall(series: list[dict[str, Any]]) -> str:
    """Barras agrupadas — receita vs despesas por mês (cashflow)."""
    labels = [s["label"] for s in series]
    revenues = [s["revenue"] for s in series]
    expenses = [s["expenses"] for s in series]
    net = [s["net"] for s in series]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Receita",
                x=labels, y=revenues,
                marker_color="#16a34a",
                hovertemplate="<b>%{x}</b><br>Receita: R$ %{y:,.2f}<extra></extra>",
            ),
            go.Bar(
                name="Despesas",
                x=labels, y=expenses,
                marker_color="#ef4444",
                hovertemplate="<b>%{x}</b><br>Despesas: R$ %{y:,.2f}<extra></extra>",
            ),
            go.Scatter(
                name="Saldo",
                x=labels, y=net,
                mode="lines+markers",
                line={"color": "#2563eb", "width": 2, "dash": "dot"},
                marker={"size": 7},
                hovertemplate="<b>%{x}</b><br>Saldo: R$ %{y:,.2f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "group",
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
        },
    )
    return _to_json(fig)


def expense_by_supplier_bar(data: list[dict[str, Any]]) -> str:
    """Barras horizontais — top fornecedores por despesa."""
    labels = [d["supplier"] for d in data]
    values = [d["amount"] for d in data]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#f97316",
                hovertemplate="<b>%{y}</b><br>R$ %{x:,.2f}<extra></extra>",
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "xaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 150, "r": 20, "t": 30, "b": 50},
        },
    )
    return _to_json(fig)


def expense_by_category_pie(data: list[dict[str, Any]]) -> str:
    """Pizza — distribuição de despesas por categoria."""
    colors = [
        "#2563eb", "#ef4444", "#16a34a", "#f97316",
        "#8b5cf6", "#0ea5e9", "#fbbf24", "#6b7280",
    ]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=[d["category"] for d in data],
                values=[d["amount"] for d in data],
                hole=0.4,
                marker={"colors": colors[: len(data)]},
                hovertemplate="<b>%{label}</b><br>R$ %{value:,.2f} (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def burn_rate_line(series: list[dict[str, Any]], burn_rate: float = 0) -> str:
    """Linha de burn rate mensal com linha de referência (média)."""
    labels = [s["label"] for s in series]
    values = [s["expenses"] for s in series]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=values,
                marker_color="#ef4444",
                name="Despesas",
                hovertemplate="<b>%{x}</b><br>R$ %{y:,.2f}<extra></extra>",
            ),
            go.Scatter(
                x=labels,
                y=[burn_rate] * len(labels),
                mode="lines",
                name="Média (burn rate)",
                line={"color": "#f97316", "width": 2, "dash": "dash"},
                hovertemplate=f"Burn rate: R$ {burn_rate:,.2f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
        },
    )
    return _to_json(fig)


def dre_grouped_bar(mrr_series: list[dict[str, Any]], expense_series: list[dict[str, Any]]) -> str:
    """Barras agrupadas — MRR vs Despesas por mês (DRE)."""
    exp_by_month = {e["month"]: e["expenses"] for e in expense_series}
    labels = [m["label"] for m in mrr_series]
    mrr_vals = [m["mrr"] for m in mrr_series]
    exp_vals = [exp_by_month.get(m["month"], 0.0) for m in mrr_series]
    ebitda_vals = [m - e for m, e in zip(mrr_vals, exp_vals, strict=False)]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Receita (MRR)",
                x=labels, y=mrr_vals,
                marker_color="#16a34a",
                hovertemplate="<b>%{x}</b><br>MRR: R$ %{y:,.2f}<extra></extra>",
            ),
            go.Bar(
                name="Despesas",
                x=labels, y=exp_vals,
                marker_color="#ef4444",
                hovertemplate="<b>%{x}</b><br>Despesas: R$ %{y:,.2f}<extra></extra>",
            ),
            go.Scatter(
                name="EBITDA",
                x=labels, y=ebitda_vals,
                mode="lines+markers",
                line={"color": "#2563eb", "width": 2},
                marker={"size": 7},
                hovertemplate="<b>%{x}</b><br>EBITDA: R$ %{y:,.2f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "group",
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
        },
    )
    return _to_json(fig)


def delinquency_trend_chart(series: list[dict[str, Any]]) -> str:
    """Barras — inadimplência não-recuperada por mês de vencimento (últimos 12 meses).

    Cada barra = valor ainda em aberto das faturas que venceram naquele mês.
    Barras mais altas à direita = crescimento recente de novos inadimplentes.
    """
    labels = [s["label"] for s in series]
    values = [s["amount"] for s in series]
    counts = [s["count"] for s in series]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=values,
                marker_color="#ef4444",
                customdata=counts,
                hovertemplate=(
                    "<b>Venc. %{x}</b><br>"
                    "Em aberto: R$ %{y:,.2f}<br>"
                    "Faturas: %{customdata}<extra></extra>"
                ),
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 60, "r": 20, "t": 10, "b": 50},
        },
    )
    return _to_json(fig)


def contract_status_stacked_chart(series: list[dict[str, Any]]) -> str:
    """Barras empilhadas — ACTIVE + BLOCKED + AWAITING_INSTALL ao longo do tempo.

    Permite visualizar a composição da base de clientes mês a mês:
    - Verde = Ativos (internet funcionando)
    - Laranja = Bloqueados por inadimplência (cobrados mas sem internet)
    - Roxo = Aguardando instalação (pipeline de novos clientes)
    """
    labels = [s["label"] for s in series]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Ativos",
                x=labels,
                y=[s["active"] for s in series],
                marker_color="#10b981",
                hovertemplate="<b>%{x}</b><br>Ativos: %{y:,d}<extra></extra>",
            ),
            go.Bar(
                name="Bloqueados",
                x=labels,
                y=[s["blocked"] for s in series],
                marker_color="#f97316",
                hovertemplate="<b>%{x}</b><br>Bloqueados: %{y:,d}<extra></extra>",
            ),
            go.Bar(
                name="Ag. Instalação",
                x=labels,
                y=[s["awaiting"] for s in series],
                marker_color="#8b5cf6",
                hovertemplate="<b>%{x}</b><br>Ag. Instalação: %{y:,d}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "margin": {"l": 50, "r": 20, "t": 10, "b": 50},
            "legend": {"orientation": "h", "y": -0.2},
        },
    )
    return _to_json(fig)


def forecast_area(
    historical: list[dict[str, Any]], forecast: list[dict[str, Any]]
) -> str:
    """Área — MRR histórico + projeção futura."""
    hist_labels = [h["label"] for h in historical]
    hist_mrr = [h["mrr"] for h in historical]
    fc_labels = [f["label"] for f in forecast]
    fc_mrr = [f["forecast_mrr"] for f in forecast]
    fc_net = [f["forecast_net"] for f in forecast]

    fig = go.Figure(
        data=[
            go.Scatter(
                name="MRR Histórico",
                x=hist_labels, y=hist_mrr,
                mode="lines+markers",
                line={"color": "#2563eb", "width": 3},
                marker={"size": 8},
                fill="tozeroy",
                fillcolor="rgba(37,99,235,0.10)",
                hovertemplate="<b>%{x}</b><br>MRR: R$ %{y:,.2f}<extra></extra>",
            ),
            go.Scatter(
                name="MRR Previsto",
                x=fc_labels, y=fc_mrr,
                mode="lines+markers",
                line={"color": "#2563eb", "width": 2, "dash": "dash"},
                marker={"size": 7, "symbol": "circle-open"},
                fill="tozeroy",
                fillcolor="rgba(37,99,235,0.05)",
                hovertemplate="<b>%{x}</b><br>Previsão MRR: R$ %{y:,.2f}<extra></extra>",
            ),
            go.Scatter(
                name="Saldo Previsto",
                x=fc_labels, y=fc_net,
                mode="lines",
                line={"color": "#16a34a", "width": 2, "dash": "dot"},
                hovertemplate="<b>%{x}</b><br>Saldo: R$ %{y:,.2f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
        },
    )
    return _to_json(fig)
