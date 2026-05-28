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
