"""Bucketização temporal de séries — dia, semana ou mês (#100, Fase 2).

Nasceu privado dentro de `aggregations.py` pra a série da triagem (#89) e virou
módulo próprio quando passou a ser o eixo do tempo de várias séries do
dashboard, agora que elas recebem uma **janela livre** em vez de `months=N`.

A regra de granularidade é a mesma de sempre (#89), pensada pra manter o gráfico
entre ~10 e ~40 pontos em qualquer preset do componente de período:

- até 31 dias  → **dia**    (Hoje, Ontem, 7d, 14d, 30d, 1 mês)
- até 120 dias → **semana** (3 meses; a semana começa na segunda)
- acima disso  → **mês**    (6m, 12m, 24m)

Sem ela, "Hoje" renderiza um ponto mensal solitário e "12 meses" vira 365 pontos
diários ilegíveis.

**O que NÃO usa isto:** DRE, MRR, Burn e Forecast. São grandezas mensais por
natureza — MRR é estoque de mês, DRE é competência mensal, previsão é mensal — e
um ponto diário ali é ruído com cara de informação. Nessas páginas a janela
escolhe *quais meses*, nunca o bucket. Decisão do Paulo em 11/08, registrada na
issue #100; não reabrir sem falar com ele.

Este módulo é Python puro (só `datetime` + `relativedelta`): não conhece Django,
nem request, nem o componente de período da camada de apresentação.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

DAY = "day"
WEEK = "week"
MONTH = "month"
AUTO = "auto"

DIA_MAX_DIAS = 31
SEMANA_MAX_DIAS = 120

GRANULARITY_LABELS = {
    DAY: "dia a dia",
    WEEK: "semana a semana",
    MONTH: "mês a mês",
}


@dataclass(frozen=True)
class Bucket:
    """Um ponto do eixo do tempo, com os dois extremos inclusivos.

    `key` é estável e ordenável (ISO da data inicial, ou `YYYY-MM` no mês), pra
    servir de chave em dict e de identificador no JSON do gráfico. `label` é o
    texto do eixo.
    """

    start: date
    end: date
    key: str
    label: str


def series_granularity(start: date, end: date) -> str:
    """Granularidade derivada do tamanho da janela (ambos os extremos inclusivos)."""
    span_days = (end - start).days + 1
    if span_days <= DIA_MAX_DIAS:
        return DAY
    if span_days <= SEMANA_MAX_DIAS:
        return WEEK
    return MONTH


def resolve_granularity(start: date, end: date, granularity: str = AUTO) -> str:
    """Resolve `auto` pelo tamanho da janela; qualquer outro valor passa direto.

    Um valor desconhecido cai no automático em vez de explodir: série de gráfico
    não é lugar de derrubar a página por causa de um querystring torto.
    """
    if granularity in (DAY, WEEK, MONTH):
        return granularity
    return series_granularity(start, end)


def bucket_start(d: date, granularity: str) -> date:
    """Início do bucket de uma data: o próprio dia, a segunda-feira ou o dia 1."""
    if granularity == MONTH:
        return d.replace(day=1)
    if granularity == DAY:
        return d
    return d - timedelta(days=d.weekday())  # segunda da semana


def next_bucket(d: date, granularity: str) -> date:
    """Início do bucket seguinte (avança 1 dia, 1 semana ou 1 mês)."""
    if granularity == MONTH:
        return d.replace(day=1) + relativedelta(months=1)
    if granularity == DAY:
        return d + timedelta(days=1)
    return d + timedelta(days=7)


def bucket_label(d: date, granularity: str) -> str:
    """Rótulo do eixo: 'mmm/yy' no mês, 'dd/mm' no dia e na semana (a segunda)."""
    if granularity == MONTH:
        return d.strftime("%b/%y")
    return d.strftime("%d/%m")


def bucket_key(d: date, granularity: str) -> str:
    """Chave estável do bucket — 'YYYY-MM' no mês, ISO da data nos demais."""
    if granularity == MONTH:
        return d.strftime("%Y-%m")
    return d.isoformat()


def build_buckets(start: date, end: date, granularity: str = AUTO) -> list[Bucket]:
    """Eixo do tempo COMPLETO da janela, sem buracos.

    O eixo é gerado a partir da janela e não dos dados: bucket sem registro vira
    zero no gráfico em vez de sumir do eixo — é a diferença entre "não houve
    nada nesse dia" e "esse dia não existe".

    O primeiro bucket começa no início do bucket que contém `start` (a segunda
    da semana, o dia 1 do mês), mesmo que isso seja antes de `start`; é o que
    faz um mês parcial continuar sendo um ponto só.
    """
    granularity = resolve_granularity(start, end, granularity)
    buckets: list[Bucket] = []
    cursor = bucket_start(start, granularity)
    last = bucket_start(end, granularity)
    while cursor <= last:
        proximo = next_bucket(cursor, granularity)
        buckets.append(
            Bucket(
                start=cursor,
                end=proximo - timedelta(days=1),
                key=bucket_key(cursor, granularity),
                label=bucket_label(cursor, granularity),
            )
        )
        cursor = proximo
    return buckets
