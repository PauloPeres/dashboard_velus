"""Teste do gráfico caixa recebido × projetado da visão executiva (#43).

Garante o contrato de binding: a série realizada vira a trace "Recebido" e a
previsão vira a trace "Projetado", com JSON Plotly válido e robusto a entradas
vazias (org recém-criada sem caixa/previsão).
"""

from __future__ import annotations

import json

from apps.dashboards.charts import cash_vs_projected_chart


def test_cash_vs_projected_emits_both_traces() -> None:
    realized = [
        {"month": "2026-04", "label": "Apr/26", "amount": 1000.0, "count": 10},
        {"month": "2026-05", "label": "May/26", "amount": 1200.0, "count": 12},
    ]
    forecast = [
        {"month": "2026-06", "label": "Jun/26", "forecast_cash": 1300.0, "is_forecast": True},
        {"month": "2026-07", "label": "Jul/26", "forecast_cash": 1350.0, "is_forecast": True},
    ]
    fig = json.loads(cash_vs_projected_chart(realized, forecast))
    traces = {t["name"]: t for t in fig["data"]}
    assert set(traces) == {"Recebido", "Projetado"}
    assert list(traces["Recebido"]["y"]) == [1000.0, 1200.0]
    assert list(traces["Projetado"]["x"]) == ["Jun/26", "Jul/26"]
    assert list(traces["Projetado"]["y"]) == [1300.0, 1350.0]


def test_cash_vs_projected_handles_empty() -> None:
    fig = json.loads(cash_vs_projected_chart([], []))
    traces = {t["name"]: t for t in fig["data"]}
    assert set(traces) == {"Recebido", "Projetado"}
    assert list(traces["Recebido"]["y"]) == []
    assert list(traces["Projetado"]["y"]) == []
