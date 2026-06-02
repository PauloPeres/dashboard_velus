"""SLA por tipo de atendimento — tempo médio de resposta/resolução e comparativo.

Função pura `compute_sla_by_category` que recebe OS já materializadas (dicts) e o
mapa `subject_id → categoria` (de `os_classification.classify_subject`), e devolve
um card por categoria de atendimento (Manutenção, Instalação, ...) com:

- **Resolução**: tempo médio `closed_at - opened_at` das OS fechadas do período.
- **Resposta/atendimento**: tempo médio `scheduled_at - opened_at` — quanto
  demoramos pra ir até o cliente. KPI-chave de Instalação.
- **Comparativo**: os mesmos tempos no período anterior (mesma duração) e o delta,
  pra mostrar se estamos melhorando (delta negativo = mais rápido).

OS são atribuídas ao período pela data de abertura (`opened_at`). Mantida pura
(sem ORM) pra ser testável isoladamente.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from apps.helpdesk.application.os_classification import OTHER, category_label


def _avg_hours(total_seconds: float, count: int) -> float:
    return round(total_seconds / count / 3600, 1) if count else 0.0


class _Acc:
    """Acumulador de tempos por categoria num período."""

    __slots__ = ("count", "res_count", "res_seconds", "resp_count", "resp_seconds")

    def __init__(self) -> None:
        self.count = 0
        self.res_seconds = 0.0
        self.res_count = 0
        self.resp_seconds = 0.0
        self.resp_count = 0


def compute_sla_by_category(
    tickets: Iterable[Mapping[str, Any]],
    subject_to_category: Mapping[str, str],
    *,
    now: datetime,
    period_days: int = 30,
) -> list[dict[str, Any]]:
    """Agrega SLA por categoria de atendimento, com comparativo vs período anterior.

    Cada item de `tickets` deve ter: `subject_id`, `status`, `opened_at`,
    `scheduled_at`, `closed_at`. `subject_to_category` mapeia o id do assunto pra
    categoria; assuntos não mapeados caem em OTHER. Retorna a lista de cards
    ordenada por volume do período atual (maior primeiro).
    """
    cur_start = now - timedelta(days=period_days)
    prev_start = now - timedelta(days=2 * period_days)

    current: dict[str, _Acc] = {}
    previous: dict[str, _Acc] = {}

    for t in tickets:
        opened_at = t.get("opened_at")
        if opened_at is None:
            continue
        if cur_start <= opened_at <= now:
            bucket = current
        elif prev_start <= opened_at < cur_start:
            bucket = previous
        else:
            continue

        cat = subject_to_category.get(str(t.get("subject_id") or "").strip(), OTHER)
        acc = bucket.setdefault(cat, _Acc())
        acc.count += 1

        closed_at = t.get("closed_at")
        if t.get("status") == "CLOSED" and closed_at is not None:
            acc.res_seconds += (closed_at - opened_at).total_seconds()
            acc.res_count += 1

        scheduled_at = t.get("scheduled_at")
        if scheduled_at is not None and scheduled_at >= opened_at:
            acc.resp_seconds += (scheduled_at - opened_at).total_seconds()
            acc.resp_count += 1

    rows: list[dict[str, Any]] = []
    for cat, acc in current.items():
        prev = previous.get(cat)
        avg_res = _avg_hours(acc.res_seconds, acc.res_count)
        avg_resp = _avg_hours(acc.resp_seconds, acc.resp_count)
        prev_res = _avg_hours(prev.res_seconds, prev.res_count) if prev else 0.0
        prev_resp = _avg_hours(prev.resp_seconds, prev.resp_count) if prev else 0.0

        # Delta só faz sentido quando há base nos dois períodos (delta < 0 = melhora).
        res_delta = (
            round(avg_res - prev_res, 1)
            if (acc.res_count and prev and prev.res_count)
            else None
        )
        resp_delta = (
            round(avg_resp - prev_resp, 1)
            if (acc.resp_count and prev and prev.resp_count)
            else None
        )

        rows.append(
            {
                "category": cat,
                "label": category_label(cat),
                "count": acc.count,
                "resolved_count": acc.res_count,
                "responded_count": acc.resp_count,
                "avg_resolution_hours": avg_res,
                "avg_response_hours": avg_resp,
                "prev_count": prev.count if prev else 0,
                "prev_avg_resolution_hours": prev_res,
                "prev_avg_response_hours": prev_resp,
                "resolution_delta_hours": res_delta,
                "response_delta_hours": resp_delta,
                "resolution_improved": res_delta is not None and res_delta < 0,
                "response_improved": resp_delta is not None and resp_delta < 0,
            }
        )

    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows
