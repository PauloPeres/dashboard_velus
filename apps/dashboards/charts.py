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


def cash_vs_projected_chart(
    realized: list[dict[str, Any]], forecast: list[dict[str, Any]]
) -> str:
    """Barras — caixa recebido (realizado) e projetado num único eixo temporal.

    Realizado em verde sólido (`amount` de compute_cash_received_series),
    projetado em verde claro tracejado (`forecast_cash` de
    compute_revenue_forecast). Dá ao executivo a leitura recebido × projetado
    em uma só visão.
    """
    real_labels = [p["label"] for p in realized]
    real_values = [p["amount"] for p in realized]
    proj_labels = [p["label"] for p in forecast]
    proj_values = [p["forecast_cash"] for p in forecast]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Recebido",
                x=real_labels, y=real_values,
                marker_color="#16a34a",
                hovertemplate="<b>%{x}</b><br>Recebido: R$ %{y:,.0f}<extra></extra>",
            ),
            go.Bar(
                name="Projetado",
                x=proj_labels, y=proj_values,
                marker_color="#86efac",
                marker_line={"color": "#16a34a", "width": 1},
                hovertemplate="<b>%{x}</b><br>Projetado: R$ %{y:,.0f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "legend": {"orientation": "h", "y": -0.2},
        },
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
    """Barras empilhadas — inadimplência por mês de vencimento, principal vs multa/juros.

    Cada barra = valor ainda em aberto das faturas que venceram naquele mês,
    separando o principal (mensalidade/MRR) do encargo por atraso (multa/juros).
    Barras mais altas à direita = crescimento recente de novos inadimplentes.

    `late_fee` costuma vir 0 (o IXC só materializa multa/juros no pagamento) —
    nesse caso a barra mostra só o principal, sem segmento de multa visível.
    """
    labels = [s["label"] for s in series]
    counts = [s["count"] for s in series]
    principal = [s.get("principal", s.get("amount", 0)) for s in series]
    late_fee = [s.get("late_fee", 0) for s in series]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Principal (MRR)",
                x=labels, y=principal,
                marker_color="#ef4444",
                customdata=counts,
                hovertemplate=(
                    "<b>Venc. %{x}</b><br>"
                    "Principal: R$ %{y:,.2f}<br>"
                    "Faturas: %{customdata}<extra></extra>"
                ),
            ),
            go.Bar(
                name="Multa/juros",
                x=labels, y=late_fee,
                marker_color="#f59e0b",
                hovertemplate="<b>Venc. %{x}</b><br>Multa/juros: R$ %{y:,.2f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "legend": {"orientation": "h", "y": -0.2, "font": {"size": 10}},
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 60, "r": 20, "t": 10, "b": 60},
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


def mrr_contracts_dual_axis(series: list[dict[str, Any]]) -> str:
    """Linha dupla — MRR (eixo esquerdo) + contratos ativos (eixo direito)."""
    labels = [p["label"] for p in series]
    mrr_vals = [p["mrr"] for p in series]
    contract_vals = [p["active_contracts"] for p in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                name="MRR",
                x=labels, y=mrr_vals,
                mode="lines+markers",
                line={"color": "#2563eb", "width": 3},
                marker={"size": 7},
                yaxis="y",
                hovertemplate="<b>%{x}</b><br>MRR: R$ %{y:,.0f}<extra></extra>",
            ),
            go.Scatter(
                name="Contratos",
                x=labels, y=contract_vals,
                mode="lines+markers",
                line={"color": "#10b981", "width": 2, "dash": "dot"},
                marker={"size": 6},
                yaxis="y2",
                hovertemplate="<b>%{x}</b><br>Contratos: %{y:,d}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f", "title": "MRR"},
            "yaxis2": {
                "title": "Contratos",
                "overlaying": "y",
                "side": "right",
                "showgrid": False,
                "tickformat": ",d",
            },
            "legend": {"orientation": "h", "y": -0.25},
        },
    )
    return _to_json(fig)


def contract_arpu_trend_line(series: list[dict[str, Any]]) -> str:
    """Linha — evolução do ARPU (ticket médio) mês a mês."""
    labels = [p["label"] for p in series]
    values = [p["arpu"] for p in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values, mode="lines+markers",
                line={"color": "#7c3aed", "width": 3},
                marker={"size": 7, "color": "#7c3aed"},
                hovertemplate="<b>%{x}</b><br>ARPU: R$ %{y:,.2f}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"}},
    )
    return _to_json(fig)


def contract_churn_trend_line(series: list[dict[str, Any]]) -> str:
    """Linha — taxa de churn (%) mensal."""
    labels = [p["label"] for p in series]
    values = [p["churn_pct"] for p in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values, mode="lines+markers",
                line={"color": "#ef4444", "width": 3},
                marker={"size": 7, "color": "#ef4444"},
                hovertemplate="<b>%{x}</b><br>Churn: %{y:.2f}%<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"ticksuffix": "%", "tickformat": ",.1f"}},
    )
    return _to_json(fig)


def equipment_field_trend_line(series: list[dict[str, Any]]) -> str:
    """Área — parque de equipamentos em campo (acumulado) por mês."""
    labels = [p["label"] for p in series]
    values = [p["count"] for p in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values, mode="lines+markers",
                fill="tozeroy",
                line={"color": "#0891b2", "width": 3},
                marker={"size": 6, "color": "#0891b2"},
                fillcolor="rgba(8,145,178,0.12)",
                hovertemplate="<b>%{x}</b><br>%{y:,d} equipamentos<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"tickformat": ",d"}},
    )
    return _to_json(fig)


def churn_by_plan_bar(data: list[dict[str, Any]]) -> str:
    """Barras horizontais — cancelamentos e receita perdida por plano."""
    labels = [d["plan"] for d in data]
    canceled = [d["canceled"] for d in data]
    revenue_lost = [d["revenue_lost"] for d in data]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Cancelamentos",
                x=canceled, y=labels, orientation="h",
                marker_color="#ef4444",
                hovertemplate=(
                    "<b>%{y}</b><br>Cancelamentos: %{x}<br>"
                    "Taxa: %{customdata:.1f}%<extra></extra>"
                ),
                customdata=[d["churn_rate"] for d in data],
                xaxis="x",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "xaxis": {"title": "Contratos cancelados"},
            "margin": {"l": 160, "r": 20, "t": 30, "b": 50},
            "showlegend": False,
        },
    )
    return _to_json(fig)


def blocked_trend_line(series: list[dict[str, Any]]) -> str:
    """Linha — evolução de contratos BLOCKED ao longo do tempo (últimos 12 meses)."""
    labels = [s["label"] for s in series]
    values = [s["blocked"] for s in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values,
                mode="lines+markers",
                line={"color": "#f97316", "width": 3},
                marker={"size": 8, "color": "#f97316"},
                fill="tozeroy",
                fillcolor="rgba(249,115,22,0.10)",
                hovertemplate="<b>%{x}</b><br>Bloqueados: %{y:,d}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"title": "Contratos bloqueados"}},
    )
    return _to_json(fig)


def blocked_duration_histogram(data: list[dict[str, Any]]) -> str:
    """Barras — distribuição de contratos BLOCKED por duração do bloqueio."""
    labels = [d["label"] for d in data]
    counts = [d["count"] for d in data]
    revenues = [d["revenue"] for d in data]
    colors = ["#fbbf24", "#f97316", "#ef4444", "#dc2626", "#991b1b"]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=counts,
                marker_color=colors[: len(labels)],
                customdata=revenues,
                hovertemplate=(
                    "<b>%{x}</b><br>Contratos: %{y}<br>"
                    "Receita em risco: R$ %{customdata:,.0f}<extra></extra>"
                ),
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "yaxis": {"title": "Contratos"},
        },
    )
    return _to_json(fig)


def churn_mrr_waterfall(series: list[dict[str, Any]]) -> str:
    """Barras agrupadas — MRR perdido vs recuperado + linha de MRR líquido mensal."""
    labels = [s["label"] for s in series]
    lost = [s["mrr_lost"] for s in series]
    recovered = [s["mrr_recovered"] for s in series]
    net = [s["net_mrr"] for s in series]
    net_colors = ["#16a34a" if v >= 0 else "#ef4444" for v in net]
    fig = go.Figure(
        data=[
            go.Bar(
                name="MRR Perdido",
                x=labels, y=lost,
                marker_color="#ef4444",
                hovertemplate="<b>%{x}</b><br>MRR perdido: R$ %{y:,.0f}<extra></extra>",
            ),
            go.Bar(
                name="MRR Recuperado",
                x=labels, y=recovered,
                marker_color="#10b981",
                hovertemplate="<b>%{x}</b><br>MRR recuperado: R$ %{y:,.0f}<extra></extra>",
            ),
            go.Scatter(
                name="MRR Líquido",
                x=labels, y=net,
                mode="lines+markers",
                line={"color": "#2563eb", "width": 2, "dash": "dot"},
                marker={"size": 8, "color": net_colors},
                hovertemplate="<b>%{x}</b><br>Líquido: R$ %{y:,.0f}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "group",
            "showlegend": True,
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "legend": {"orientation": "h", "y": -0.25},
        },
    )
    return _to_json(fig)


def churn_reason_pareto(data: list[dict[str, Any]]) -> str:
    """Barras horizontais + linha de % acumulado (Pareto) por motivo de cancelamento."""
    labels = [d["label"] for d in data]
    mrr_vals = [d["mrr_lost"] for d in data]
    pct_acc = [d["pct_acc"] for d in data]
    # Cores: controlável=vermelho, não-controlável=cinza, neutro=laranja
    color_map = {True: "#ef4444", False: "#9ca3af", None: "#f97316"}
    bar_colors = [color_map.get(d["controlavel"], "#6b7280") for d in data]

    fig = go.Figure(
        data=[
            go.Bar(
                name="MRR perdido",
                x=mrr_vals, y=labels, orientation="h",
                marker_color=bar_colors,
                customdata=[[d["count"], d["pct"]] for d in data],
                hovertemplate=(
                    "<b>%{y}</b><br>MRR: R$ %{x:,.0f}<br>"
                    "Contratos: %{customdata[0]}<br>%{customdata[1]:.1f}% do total<extra></extra>"
                ),
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "xaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 200, "r": 60, "t": 30, "b": 50},
            "showlegend": False,
            "height": 400,
        },
    )
    return _to_json(fig)


def ltv_histogram(data: list[dict[str, Any]]) -> str:
    """Barras — histograma de LTV dos contratos cancelados."""
    labels = [d["label"] for d in data]
    counts = [d["count"] for d in data]
    avg_mrr = [d["avg_mrr"] for d in data]
    colors = ["#fbbf24", "#f97316", "#ef4444", "#dc2626"]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=counts,
                marker_color=colors[: len(labels)],
                customdata=avg_mrr,
                hovertemplate=(
                    "<b>%{x}</b><br>Contratos: %{y}<br>"
                    "Ticket médio: R$ %{customdata:,.0f}<extra></extra>"
                ),
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "yaxis": {"title": "Contratos cancelados"},
        },
    )
    return _to_json(fig)


def churn_plan_risk_scatter(data: list[dict[str, Any]], overall_rate: float = 0) -> str:
    """Scatter plot: eixo X = base do plano, Y = taxa de churn, tamanho = MRR perdido.

    Cada plano é um ponto. A linha horizontal mostra a taxa média geral.
    Pontos acima da linha = churn acima da média (preocupante).
    Pontos à direita = planos grandes (maior impacto absoluto).
    """
    with_rate = [d for d in data if d.get("churn_rate") is not None and d.get("base", 0) > 0]
    if not with_rate:
        return _to_json(go.Figure())

    x = [d["base"] for d in with_rate]
    y = [d["churn_rate"] for d in with_rate]
    labels = [d["plan"] for d in with_rate]
    sizes = [max(8, min(50, d["count"] * 2)) for d in with_rate]
    risk = [d.get("risk_index") or 1.0 for d in with_rate]
    colors = [
        "#ef4444" if r > 1.5 else "#f97316" if r > 1.0 else "#10b981"
        for r in risk
    ]
    custom = [[d["count"], d["mrr_lost"], d.get("risk_index") or 0] for d in with_rate]

    x_max = max(x) * 1.15 if x else 10
    y_max = max(y) * 1.3 if y else 5

    fig = go.Figure(
        data=[
            go.Scatter(
                x=x, y=y,
                mode="markers+text",
                text=[lbl[:15] for lbl in labels],
                textposition="top center",
                textfont={"size": 9},
                marker={
                    "size": sizes,
                    "color": colors,
                    "opacity": 0.75,
                    "line": {"width": 1, "color": "#ffffff"},
                },
                customdata=custom,
                hovertemplate=(
                    "<b>%{text}</b><br>"
                    "Base: %{x:,d} contratos<br>"
                    "Churn: %{y:.1f}%<br>"
                    "Cancelamentos: %{customdata[0]}<br>"
                    "MRR perdido: R$ %{customdata[1]:,.0f}<br>"
                    "Risco: %{customdata[2]:.2f}×<extra></extra>"
                ),
            ),
            # Linha de benchmark — taxa global
            go.Scatter(
                x=[0, x_max],
                y=[overall_rate, overall_rate],
                mode="lines",
                line={"color": "#6b7280", "width": 1.5, "dash": "dash"},
                name=f"Média geral ({overall_rate:.1f}%)",
                hovertemplate=f"Taxa média: {overall_rate:.1f}%<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "xaxis": {"title": "Contratos na base", "tickformat": ",d"},
            "yaxis": {"title": "Taxa de churn (%)", "ticksuffix": "%"},
            "legend": {"orientation": "h", "y": -0.3},
            "margin": {"l": 60, "r": 20, "t": 30, "b": 70},
        },
    )
    return _to_json(fig)


def churn_logo_line(series: list[dict[str, Any]]) -> str:
    """Linha dupla — cancelamentos vs novas ativações por mês."""
    labels = [s["label"] for s in series]
    churned = [s["logo_churn"] for s in series]
    new_logos = [s["new_logos"] for s in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                name="Cancelamentos",
                x=labels, y=churned,
                mode="lines+markers",
                line={"color": "#ef4444", "width": 3},
                marker={"size": 8},
                hovertemplate="<b>%{x}</b><br>Cancelados: %{y}<extra></extra>",
            ),
            go.Scatter(
                name="Novas ativações",
                x=labels, y=new_logos,
                mode="lines+markers",
                line={"color": "#10b981", "width": 3},
                marker={"size": 8},
                hovertemplate="<b>%{x}</b><br>Ativações: %{y}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"title": "Contratos"},
            "legend": {"orientation": "h", "y": -0.25},
        },
    )
    return _to_json(fig)


def people_expenses_stacked_bar(data: dict[str, Any]) -> str:
    """Barras empilhadas — despesas por pessoa por mês (prestadores PJ + coletivo)."""
    labels = data.get("month_labels", [])
    people = data.get("people", [])
    mao = data.get("mao_de_obra", {})

    COLORS = [
        "#2563eb", "#dc2626", "#d97706", "#16a34a", "#7c3aed",
        "#db2777", "#0891b2", "#65a30d", "#ea580c", "#0f766e",
    ]

    traces: list[Any] = []
    for i, person in enumerate(people):
        name_display = person["name"].title()
        traces.append(
            go.Bar(
                name=name_display,
                x=labels,
                y=person["monthly"],
                marker_color=COLORS[i % len(COLORS)],
                hovertemplate=f"<b>%{{x}}</b><br>{name_display}<br>R$ %{{y:,.0f}}<extra></extra>",
            )
        )

    mao_amounts = mao.get("monthly", [])
    if mao_amounts and any(v > 0 for v in mao_amounts):
        traces.append(
            go.Bar(
                name="Mão de Obra (coletivo)",
                x=labels,
                y=mao_amounts,
                marker_color="#9ca3af",
                hovertemplate="<b>%{x}</b><br>Mão de Obra<br>R$ %{y:,.0f}<extra></extra>",
            )
        )

    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "legend": {"orientation": "h", "y": -0.3, "font": {"size": 10}},
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 60, "r": 20, "t": 30, "b": 110},
        },
    )
    return _to_json(fig)


def mao_de_obra_stacked_bar(data: dict[str, Any]) -> str:
    """Barras empilhadas — Mão de Obra Terceirizada por categoria (descrição) mês a mês."""
    labels = data.get("month_labels", [])
    categories = data.get("by_category", [])

    # Paleta diferente do gráfico de pessoas — tons de laranja/âmbar/terra
    COLORS = [
        "#ea580c", "#d97706", "#b45309", "#c2410c", "#92400e",
        "#f97316", "#fb923c", "#fbbf24", "#a16207", "#78350f",
    ]

    traces: list[Any] = []
    for i, cat in enumerate(categories):
        if not any(v > 0 for v in cat["monthly"]):
            continue
        # Trunca o label se muito longo para a legenda
        name = cat["label"]
        if len(name) > 40:
            name = name[:37] + "…"
        traces.append(
            go.Bar(
                name=name,
                x=labels,
                y=cat["monthly"],
                marker_color=COLORS[i % len(COLORS)],
                hovertemplate=f"<b>%{{x}}</b><br>{name}<br>R$ %{{y:,.0f}}<extra></extra>",
            )
        )

    if not traces:
        traces = [go.Bar(x=[], y=[])]

    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "legend": {"orientation": "h", "y": -0.35, "font": {"size": 10}},
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 60, "r": 20, "t": 30, "b": 120},
        },
    )
    return _to_json(fig)


def recovery_by_aging_chart(by_aging: list[dict[str, Any]]) -> str:
    """Barras — taxa de recuperação (%) por profundidade do atraso.

    Cada barra = % do valor inadimplido naquele bucket de aging que acabou sendo
    recuperado (pago em atraso). A curva descendente esperada — quanto mais velho
    o atraso, menor a recuperação — é o insight central da inadimplência.
    """
    labels = [b["label"] for b in by_aging]
    pct = [b["pct"] for b in by_aging]
    recovered = [b["recovered"] for b in by_aging]
    total = [b["total"] for b in by_aging]
    counts = [b["count"] for b in by_aging]
    # Verde forte → vermelho conforme o atraso aumenta (recuperação piora)
    colors = ["#10b981", "#fbbf24", "#f97316", "#ef4444"]
    custom = [[r, t, c] for r, t, c in zip(recovered, total, counts, strict=False)]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=pct,
                marker_color=colors[: len(labels)],
                customdata=custom,
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "Recuperação: %{y:.1f}%<br>"
                    "Recuperado: R$ %{customdata[0]:,.2f}<br>"
                    "Inadimpliu: R$ %{customdata[1]:,.2f}<br>"
                    "Faturas: %{customdata[2]}<extra></extra>"
                ),
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "yaxis": {"ticksuffix": "%", "range": [0, 100], "title": "Recuperação"},
            "margin": {"l": 60, "r": 20, "t": 10, "b": 50},
        },
    )
    return _to_json(fig)


def dre_by_account_stacked_bar(data: dict[str, Any]) -> str:
    """Barras empilhadas — despesas por conta do planejamento IXC + linha de receita."""
    labels = data.get("month_labels", [])
    categories = data.get("categories", [])
    revenue_series = data.get("revenue_series", [])
    months_keys = data.get("months", [])  # "YYYY-MM" — para alinhar receita

    # Build month → mrr lookup
    rev_by_month: dict[str, float] = {r["month"]: float(r["mrr"]) for r in revenue_series}
    rev_values = [rev_by_month.get(mk, 0.0) for mk in months_keys]

    COLORS = [
        "#ef4444", "#f97316", "#d97706", "#16a34a", "#0891b2",
        "#2563eb", "#7c3aed", "#db2777", "#64748b", "#0f766e",
    ]

    traces: list[Any] = []
    for i, cat in enumerate(categories):
        traces.append(
            go.Bar(
                name=cat["label"],
                x=labels,
                y=cat["monthly"],
                marker_color=COLORS[i % len(COLORS)],
                hovertemplate=f"<b>%{{x}}</b><br>{cat['label']}<br>R$ %{{y:,.0f}}<extra></extra>",
            )
        )

    if rev_values and any(v > 0 for v in rev_values):
        traces.append(
            go.Scatter(
                name="Receita (MRR)",
                x=labels,
                y=rev_values,
                mode="lines+markers",
                line={"color": "#10b981", "width": 2, "dash": "dot"},
                marker={"size": 7},
                hovertemplate="<b>%{x}</b><br>Receita: R$ %{y:,.0f}<extra></extra>",
            )
        )

    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "legend": {"orientation": "h", "y": -0.35, "font": {"size": 10}},
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "margin": {"l": 60, "r": 20, "t": 30, "b": 120},
        },
    )
    return _to_json(fig)


def compromissos_futuros_stacked_bar(data: dict[str, Any]) -> str:
    """Barras empilhadas — compromissos futuros por camada + saldo acumulado.

    Empilha as despesas OPEN futuras por camada gerencial (recorrente, dívida
    bancária, outras dívidas, M&A, capex) e sobrepõe a linha do saldo a quitar
    (eixo secundário), que mostra a desalavancagem mês a mês conforme as parcelas
    estruturais se encerram.
    """
    labels = data.get("month_labels", [])
    tiers = data.get("tiers", {})
    cumulative = data.get("cumulative", [])

    # Recorrente agrega operacional + impostos + outras (ruído de fundo);
    # dívida e M&A ganham destaque por serem as frentes que um dia acabam.
    recorrente = [
        a + b + c
        for a, b, c in zip(
            tiers.get("operacional", {}).get("monthly", [0.0] * len(labels)),
            tiers.get("impostos", {}).get("monthly", [0.0] * len(labels)),
            tiers.get("outras", {}).get("monthly", [0.0] * len(labels)),
            strict=False,
        )
    ]
    # Dívida separada em bancária (instituições financeiras) × outras (ex.:
    # empréstimo de sócio, despesas não operacionais).
    divida_split = data.get("divida_split", {})
    bar_specs = [
        ("Operacional / Recorrente", recorrente, "#94a3b8"),
        ("Dívida Bancária", divida_split.get("banco", {}).get("monthly", []), "#b91c1c"),
        ("Outras Dívidas", divida_split.get("outras", {}).get("monthly", []), "#fb923c"),
        ("M&A (Aquisições)", tiers.get("investimento", {}).get("monthly", []), "#7c3aed"),
        ("Imobilizado / Capex", tiers.get("capex", {}).get("monthly", []), "#3b82f6"),
    ]

    traces: list[Any] = []
    for name, values, color in bar_specs:
        if values and any(v > 0 for v in values):
            traces.append(
                go.Bar(
                    name=name,
                    x=labels,
                    y=values,
                    marker_color=color,
                    hovertemplate=f"<b>%{{x}}</b><br>{name}<br>R$ %{{y:,.0f}}<extra></extra>",
                )
            )

    if cumulative and any(v > 0 for v in cumulative):
        traces.append(
            go.Scatter(
                name="Saldo a quitar",
                x=labels,
                y=cumulative,
                mode="lines",
                yaxis="y2",
                line={"color": "#0f766e", "width": 2, "dash": "dot"},
                hovertemplate="<b>%{x}</b><br>Saldo a quitar: R$ %{y:,.0f}<extra></extra>",
            )
        )

    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "legend": {"orientation": "h", "y": -0.35, "font": {"size": 10}},
            "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f"},
            "yaxis2": {
                "overlaying": "y",
                "side": "right",
                "tickprefix": "R$ ",
                "tickformat": ",.0f",
                "showgrid": False,
            },
            "margin": {"l": 60, "r": 60, "t": 30, "b": 120},
        },
    )
    return _to_json(fig)


def cash_mismatch_chart(data: dict[str, Any]) -> str:
    """Descasamento de caixa por dia do mês — entradas × saídas + saldo acum.

    Barras divergentes: verde pra cima = recebimentos, vermelho pra baixo =
    pagamentos (média por dia do mês). Linha no eixo secundário = saldo do mês
    acumulado (parte de zero), que mergulha quando as saídas se concentram e só
    se recupera quando os recebimentos chegam — é o "buraco" de liquidez.
    """
    labels = data.get("day_labels", [])
    inflow = data.get("inflow", [])
    outflow = data.get("outflow", [])
    cumulative = data.get("cumulative", [])
    today_day = data.get("today_day")

    traces: list[Any] = [
        go.Bar(
            name="Entra (recebimentos)",
            x=labels,
            y=inflow,
            marker_color="#10b981",
            hovertemplate="Dia %{x}<br>Entra: R$ %{y:,.0f}<extra></extra>",
        ),
        go.Bar(
            name="Sai (pagamentos)",
            x=labels,
            y=[-v for v in outflow],
            marker_color="#ef4444",
            customdata=outflow,
            hovertemplate="Dia %{x}<br>Sai: R$ %{customdata:,.0f}<extra></extra>",
        ),
        go.Scatter(
            name="Saldo do mês (acumulado)",
            x=labels,
            y=cumulative,
            mode="lines",
            yaxis="y2",
            line={"color": "#0f766e", "width": 2.5},
            hovertemplate="Dia %{x}<br>Saldo acum.: R$ %{y:,.0f}<extra></extra>",
        ),
    ]

    layout: dict[str, Any] = {
        **_LAYOUT_BASE,
        "barmode": "relative",
        "showlegend": True,
        "legend": {"orientation": "h", "y": -0.2, "font": {"size": 10}},
        "xaxis": {"title": "Dia do mês", "tickmode": "linear", "dtick": 2},
        "yaxis": {"tickprefix": "R$ ", "tickformat": ",.0f", "title": "Por dia"},
        "yaxis2": {
            "overlaying": "y",
            "side": "right",
            "tickprefix": "R$ ",
            "tickformat": ",.0f",
            "showgrid": False,
            "title": "Saldo acum.",
            "zeroline": True,
            "zerolinecolor": "#94a3b8",
        },
        "margin": {"l": 60, "r": 60, "t": 30, "b": 80},
    }

    # Linha vertical "hoje" no modo híbrido: separa o efetuado do a vencer.
    if today_day:
        x_today = f"{int(today_day):02d}"
        layout["shapes"] = [
            {
                "type": "line",
                "x0": x_today, "x1": x_today,
                "yref": "paper", "y0": 0, "y1": 1,
                "line": {"color": "#64748b", "width": 1.5, "dash": "dash"},
            }
        ]
        layout["annotations"] = [
            {
                "x": x_today, "xref": "x",
                "yref": "paper", "y": 1.02,
                "text": "hoje", "showarrow": False,
                "font": {"size": 10, "color": "#64748b"},
            }
        ]

    fig = go.Figure(data=traces, layout=layout)
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
    # Banda de cenários (otimista/pessimista); .get p/ compat retroativa.
    fc_opt = [f.get("forecast_mrr_optimistic", f["forecast_mrr"]) for f in forecast]
    fc_pess = [f.get("forecast_mrr_pessimistic", f["forecast_mrr"]) for f in forecast]
    has_band = any(o != m or p != m for o, p, m in zip(fc_opt, fc_pess, fc_mrr))

    band_traces = []
    if has_band:
        band_traces = [
            go.Scatter(
                name="Cenário Pessimista",
                x=fc_labels, y=fc_pess,
                mode="lines",
                line={"color": "rgba(37,99,235,0)", "width": 0},
                hovertemplate="<b>%{x}</b><br>Pessimista: R$ %{y:,.2f}<extra></extra>",
                showlegend=False,
            ),
            go.Scatter(
                name="Cenário (otimista–pessimista)",
                x=fc_labels, y=fc_opt,
                mode="lines",
                line={"color": "rgba(37,99,235,0)", "width": 0},
                fill="tonexty",
                fillcolor="rgba(37,99,235,0.12)",
                hovertemplate="<b>%{x}</b><br>Otimista: R$ %{y:,.2f}<extra></extra>",
            ),
        ]

    fig = go.Figure(
        data=[
            *band_traces,
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


def ticket_volume_trend(series: list[dict[str, Any]]) -> str:
    """Linha dupla — chamados abertos vs fechados por mes."""
    labels = [s["label"] for s in series]
    opened = [s["opened"] for s in series]
    closed = [s["closed"] for s in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                name="Abertos",
                x=labels, y=opened,
                mode="lines+markers",
                line={"color": "#ef4444", "width": 3},
                marker={"size": 8},
                hovertemplate="<b>%{x}</b><br>Abertos: %{y}<extra></extra>",
            ),
            go.Scatter(
                name="Fechados",
                x=labels, y=closed,
                mode="lines+markers",
                line={"color": "#10b981", "width": 3},
                marker={"size": 8},
                hovertemplate="<b>%{x}</b><br>Fechados: %{y}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"title": "Chamados"},
            "legend": {"orientation": "h", "y": -0.25},
        },
    )
    return _to_json(fig)


def ticket_priority_pie(data: list[dict[str, Any]]) -> str:
    """Donut — distribuicao de chamados abertos por prioridade."""
    colors_map = {
        "URGENT": "#dc2626",
        "HIGH": "#f97316",
        "NORMAL": "#2563eb",
        "LOW": "#10b981",
        "UNKNOWN": "#9ca3af",
    }
    labels = [d["priority"] for d in data]
    values = [d["count"] for d in data]
    colors = [colors_map.get(d.get("priority_key", ""), "#6b7280") for d in data]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                marker={"colors": colors},
                hovertemplate="<b>%{label}</b><br>%{value} chamados (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def os_volume_by_type(rows: list[dict[str, Any]]) -> str:
    """Barras horizontais — volume de OS por tipo (assunto)."""
    # Ordena ascendente pra barra mais longa ficar no topo do gráfico.
    ordered = sorted(rows, key=lambda r: r["total"])
    labels = [r["subject"] for r in ordered]
    values = [r["total"] for r in ordered]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#6366f1",
                hovertemplate="<b>%{y}</b><br>%{x} OS<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"title": "OS"}},
    )
    return _to_json(fig)


def os_avg_resolution_by_type(rows: list[dict[str, Any]]) -> str:
    """Barras horizontais — tempo médio de resolução (horas) por tipo de OS."""
    # Só tipos com resolução medida; ordena ascendente.
    measured = [r for r in rows if r["avg_res_hours"] > 0]
    ordered = sorted(measured, key=lambda r: r["avg_res_hours"])
    labels = [r["subject"] for r in ordered]
    values = [round(r["avg_res_hours"], 1) for r in ordered]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#f59e0b",
                hovertemplate="<b>%{y}</b><br>%{x:.1f}h em média<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"title": "Horas", "tickformat": ",.0f"}},
    )
    return _to_json(fig)


def os_monthly_trend(series: list[dict[str, Any]]) -> str:
    """Linha — OS abertas por mês."""
    labels = [s["label"] for s in series]
    values = [s["opened"] for s in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values, mode="lines+markers",
                line={"color": "#6366f1", "width": 3},
                marker={"size": 8},
                hovertemplate="<b>%{x}</b><br>%{y} OS abertas<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"title": "OS abertas"}},
    )
    return _to_json(fig)


def os_status_pie(data: list[dict[str, Any]]) -> str:
    """Donut — distribuição de OS por status."""
    colors_map = {
        "OPEN": "#2563eb",
        "SCHEDULED": "#8b5cf6",
        "IN_PROGRESS": "#f59e0b",
        "CLOSED": "#10b981",
        "FORWARDED": "#06b6d4",
        "UNKNOWN": "#9ca3af",
    }
    labels = [d["status"] for d in data]
    values = [d["count"] for d in data]
    colors = [colors_map.get(d.get("status_key", ""), "#6b7280") for d in data]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                marker={"colors": colors},
                hovertemplate="<b>%{label}</b><br>%{value} OS (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def technician_production_bar(rows: list[dict[str, Any]]) -> str:
    """Barras horizontais — produção (volume de OS) por técnico."""
    ordered = sorted(rows, key=lambda r: r["total"])
    labels = [r["technician"] for r in ordered]
    values = [r["total"] for r in ordered]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#0ea5e9",
                hovertemplate="<b>%{y}</b><br>%{x} OS<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"title": "OS atendidas"}},
    )
    return _to_json(fig)


def technician_solution_bar(rows: list[dict[str, Any]]) -> str:
    """Barras horizontais — taxa de solução (%) por técnico, colorida por faixa."""
    ordered = sorted(rows, key=lambda r: r["solution_rate"])
    labels = [r["technician"] for r in ordered]
    values = [r["solution_rate"] for r in ordered]
    colors = [
        "#ef4444" if v < 70 else "#f59e0b" if v < 85 else "#10b981"
        for v in values
    ]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color=colors,
                hovertemplate="<b>%{y}</b><br>%{x:.1f}% resolvidas<extra></extra>",
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "xaxis": {"title": "Taxa de solução (%)", "range": [0, 100]},
        },
    )
    return _to_json(fig)


def technician_monthly_lines(data: dict[str, Any]) -> str:
    """Linhas — evolução da produção (OS) mês a mês dos principais técnicos.

    `data` traz `labels` (eixo do tempo) e `per_tech`: cada item com `technician`
    (nome resolvido) e `values` (OS por mês). Uma linha por técnico.
    """
    labels = data.get("labels", [])
    per_tech = data.get("per_tech", [])
    COLORS = [
        "#2563eb", "#dc2626", "#d97706", "#16a34a", "#7c3aed",
        "#db2777", "#0891b2", "#65a30d",
    ]
    traces = [
        go.Scatter(
            name=t["technician"],
            x=labels,
            y=t["values"],
            mode="lines+markers",
            line={"color": COLORS[i % len(COLORS)], "width": 2},
            marker={"size": 6},
            hovertemplate=f"<b>{t['technician']}</b><br>%{{x}}: %{{y}} OS<extra></extra>",
        )
        for i, t in enumerate(per_tech)
    ]
    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"title": "OS abertas"},
            "legend": {"orientation": "h", "y": -0.25, "font": {"size": 10}},
            "margin": {"l": 50, "r": 20, "t": 30, "b": 80},
        },
    )
    return _to_json(fig)


def technician_category_stacked(data: dict[str, Any]) -> str:
    """Barras horizontais empilhadas — OS por categoria de atendimento por técnico.

    `data` traz `categories` (lista de {key, label}) e `rows`: cada item com
    `technician` e `counts` (dict categoria→nº). Mostra o mix de tipos atendidos.
    """
    rows = data.get("rows", [])
    categories = data.get("categories", [])
    COLORS = [
        "#0ea5e9", "#6366f1", "#f59e0b", "#10b981", "#ef4444",
        "#8b5cf6", "#ec4899", "#9ca3af",
    ]
    names = [r["technician"] for r in rows]
    traces = [
        go.Bar(
            name=cat["label"],
            y=names,
            x=[r["counts"].get(cat["key"], 0) for r in rows],
            orientation="h",
            marker_color=COLORS[i % len(COLORS)],
            hovertemplate=f"<b>%{{y}}</b><br>{cat['label']}: %{{x}} OS<extra></extra>",
        )
        for i, cat in enumerate(categories)
    ]
    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "xaxis": {"title": "OS atendidas"},
            "legend": {"orientation": "h", "y": -0.2, "font": {"size": 10}},
            "margin": {"l": 120, "r": 20, "t": 30, "b": 70},
        },
    )
    return _to_json(fig)


def network_history_lines(data: dict[str, Any]) -> str:
    """Série temporal — evolução de conexões online/offline e banda por snapshot.

    `data` traz `labels` (eixo do tempo) e as séries `online`, `offline` e
    `bandwidth_gb`. Banda vai num eixo Y secundário (escala diferente de nº de
    conexões). Sem snapshots ainda, retorna figura vazia (fallback no template).
    """
    labels = data.get("labels", [])
    traces = [
        go.Scatter(
            name="Online",
            x=labels,
            y=data.get("online", []),
            mode="lines",
            line={"color": "#10b981", "width": 2},
            hovertemplate="%{x}<br>Online: %{y}<extra></extra>",
        ),
        go.Scatter(
            name="Offline",
            x=labels,
            y=data.get("offline", []),
            mode="lines",
            line={"color": "#f97316", "width": 2},
            hovertemplate="%{x}<br>Offline: %{y}<extra></extra>",
        ),
        go.Scatter(
            name="Banda (GB)",
            x=labels,
            y=data.get("bandwidth_gb", []),
            mode="lines",
            yaxis="y2",
            line={"color": "#2563eb", "width": 2, "dash": "dot"},
            hovertemplate="%{x}<br>Banda: %{y} GB<extra></extra>",
        ),
    ]
    fig = go.Figure(
        data=traces,
        layout={
            **_LAYOUT_BASE,
            "showlegend": True,
            "yaxis": {"title": "Conexões"},
            "yaxis2": {
                "title": "Banda (GB)",
                "overlaying": "y",
                "side": "right",
                "showgrid": False,
            },
            "legend": {"orientation": "h", "y": -0.25, "font": {"size": 10}},
            "margin": {"l": 50, "r": 55, "t": 30, "b": 60},
        },
    )
    return _to_json(fig)


def connection_status_pie(data: list[dict[str, Any]]) -> str:
    """Donut — distribuicao de conexoes por status (online/offline/bloqueado)."""
    colors_map = {
        "ONLINE": "#10b981",
        "OFFLINE": "#f97316",
        "BLOCKED": "#ef4444",
        "UNKNOWN": "#9ca3af",
    }
    labels = [d["status"] for d in data]
    values = [d["count"] for d in data]
    colors = [colors_map.get(d.get("status_key", ""), "#6b7280") for d in data]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                marker={"colors": colors},
                hovertemplate="<b>%{label}</b><br>%{value} conexões (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def connections_by_nas_bar(data: list[dict[str, Any]]) -> str:
    """Barras horizontais — conexoes por concentrador (NAS/OLT)."""
    labels = [d["nas_ip"] for d in data]
    values = [d["count"] for d in data]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#0ea5e9",
                hovertemplate="<b>%{y}</b><br>%{x} conexões<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"title": "Conexões"}},
    )
    return _to_json(fig)


def sales_funnel_chart(stages: list[dict[str, Any]]) -> str:
    """Funil de vendas — leads → negociações → ganhos (barras horizontais)."""
    labels = [s["stage"] for s in stages]
    values = [s["count"] for s in stages]
    colors = ["#3b82f6", "#6366f1", "#10b981"]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color=colors[: len(labels)],
                text=values, textposition="auto",
                hovertemplate="<b>%{y}</b><br>%{x}<extra></extra>",
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "yaxis": {"autorange": "reversed"},
            "xaxis": {"title": ""},
        },
    )
    return _to_json(fig)


def net_adds_bar_chart(series: list[dict[str, Any]]) -> str:
    """Barras — net adds por mês (verde positivo, vermelho negativo)."""
    labels = [p["label"] for p in series]
    values = [p["net"] for p in series]
    colors = ["#10b981" if v >= 0 else "#ef4444" for v in values]
    fig = go.Figure(
        data=[
            go.Bar(
                x=labels, y=values,
                marker_color=colors,
                hovertemplate="<b>%{x}</b><br>Net: %{y}<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"title": "Net adds"}},
    )
    return _to_json(fig)


def lead_origin_pie(data: list[dict[str, Any]]) -> str:
    """Donut — distribuição de leads por canal de origem."""
    labels = [d["origin"] for d in data]
    values = [d["count"] for d in data]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                hovertemplate="<b>%{label}</b><br>%{value} leads (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def bandwidth_top_consumers_bar(data: list[dict[str, Any]]) -> str:
    """Barras horizontais empilhadas — top consumidores (download + upload em GB)."""
    labels = [d["customer_name"] for d in data]
    download_gb = [round((d["download_bytes"] or 0) / 1024**3, 2) for d in data]
    upload_gb = [round((d["upload_bytes"] or 0) / 1024**3, 2) for d in data]
    fig = go.Figure(
        data=[
            go.Bar(
                x=download_gb, y=labels, orientation="h",
                name="Download", marker_color="#0ea5e9",
                hovertemplate="<b>%{y}</b><br>Download: %{x} GB<extra></extra>",
            ),
            go.Bar(
                x=upload_gb, y=labels, orientation="h",
                name="Upload", marker_color="#6366f1",
                hovertemplate="<b>%{y}</b><br>Upload: %{x} GB<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "yaxis": {"autorange": "reversed"},
            "xaxis": {"title": "GB"},
            "showlegend": True,
        },
    )
    return _to_json(fig)


def churn_risk_level_pie(summary: dict[str, Any]) -> str:
    """Donut — distribuição de clientes em risco por nível (alto/médio/baixo)."""
    labels = ["Alto", "Médio", "Baixo"]
    values = [summary["high"], summary["medium"], summary["low"]]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.5,
                marker={"colors": ["#dc2626", "#f97316", "#fbbf24"]},
                hovertemplate="<b>%{label}</b><br>%{value} clientes (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def churn_risk_signal_bar(distribution: list[dict[str, Any]]) -> str:
    """Barras horizontais — quantos clientes disparam cada sinal de risco."""
    labels = [d["label"] for d in distribution]
    values = [d["count"] for d in distribution]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#ef4444",
                hovertemplate="<b>%{y}</b><br>%{x} clientes<extra></extra>",
            )
        ],
        layout={
            **_LAYOUT_BASE,
            "yaxis": {"autorange": "reversed"},
            "xaxis": {"title": "Clientes"},
        },
    )
    return _to_json(fig)


# =============================================================================
# Atendimento (Opa! Suite) — triagem por departamento (issue #48)
# =============================================================================
def atendimento_volume_by_departamento(rows: list[dict[str, Any]]) -> str:
    """Barras horizontais — volume de atendimentos por departamento."""
    ordered = sorted(rows, key=lambda r: r["total"])
    labels = [r["nome"] for r in ordered]
    values = [r["total"] for r in ordered]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#6366f1",
                hovertemplate="<b>%{y}</b><br>%{x} atendimentos<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"title": "Atendimentos"}},
    )
    return _to_json(fig)


def atendimento_status_pie(data: list[dict[str, Any]]) -> str:
    """Donut — distribuição de atendimentos por status."""
    colors_map = {
        "OPEN": "#2563eb",
        "IN_PROGRESS": "#f59e0b",
        "CLOSED": "#10b981",
        "UNKNOWN": "#9ca3af",
    }
    labels = [d["status"] for d in data]
    values = [d["count"] for d in data]
    colors = [colors_map.get(d.get("status_key", ""), "#6b7280") for d in data]
    fig = go.Figure(
        data=[
            go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                marker={"colors": colors},
                hovertemplate="<b>%{label}</b><br>%{value} (%{percent})<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "showlegend": True},
    )
    return _to_json(fig)


def atendimento_monthly_trend(series: list[dict[str, Any]]) -> str:
    """Linha — atendimentos abertos por mês."""
    labels = [s["label"] for s in series]
    values = [s["count"] for s in series]
    fig = go.Figure(
        data=[
            go.Scatter(
                x=labels, y=values, mode="lines+markers",
                line={"color": "#6366f1", "width": 3},
                marker={"size": 8},
                hovertemplate="<b>%{x}</b><br>%{y} atendimentos<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "yaxis": {"title": "Atendimentos"}},
    )
    return _to_json(fig)


def bot_deflection_trend(series: list[dict[str, Any]]) -> str:
    """Barras empilhadas diárias — resolvidos pelo bot (deflexão) vs humano.

    Verde = bot resolveu sozinho (sem precisar de humano); roxo = encaminhado ao
    atendente humano. A altura total é o volume de atendimentos abertos no dia.
    """
    labels = [s["label"] for s in series]
    fig = go.Figure(
        data=[
            go.Bar(
                name="Resolvido pelo bot",
                x=labels,
                y=[s["bot"] for s in series],
                marker_color="#10b981",
                hovertemplate="<b>%{x}</b><br>Bot: %{y:,d}<extra></extra>",
            ),
            go.Bar(
                name="Encaminhado ao humano",
                x=labels,
                y=[s["human"] for s in series],
                marker_color="#6366f1",
                hovertemplate="<b>%{x}</b><br>Humano: %{y:,d}<extra></extra>",
            ),
        ],
        layout={
            **_LAYOUT_BASE,
            "barmode": "stack",
            "showlegend": True,
            "margin": {"l": 50, "r": 20, "t": 10, "b": 50},
            "legend": {"orientation": "h", "y": -0.2},
            "yaxis": {"title": "Atendimentos"},
        },
    )
    return _to_json(fig)


def atendimento_top_motivos(rows: list[dict[str, Any]]) -> str:
    """Barras horizontais — motivos mais frequentes de atendimento."""
    ordered = sorted(rows, key=lambda r: r["count"])
    labels = [r["motivo"] for r in ordered]
    values = [r["count"] for r in ordered]
    fig = go.Figure(
        data=[
            go.Bar(
                x=values, y=labels, orientation="h",
                marker_color="#06b6d4",
                hovertemplate="<b>%{y}</b><br>%{x} atendimentos<extra></extra>",
            )
        ],
        layout={**_LAYOUT_BASE, "xaxis": {"title": "Atendimentos"}},
    )
    return _to_json(fig)
