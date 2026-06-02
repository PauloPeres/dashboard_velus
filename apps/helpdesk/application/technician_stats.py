"""Agregação de métricas por técnico — produção, qualidade e retorno.

Função pura `compute_technician_stats` que recebe uma lista de OS (dicts já
materializados do queryset) e devolve um ranking por técnico combinando:

- **Produção**: nº de OS atendidas e tipo de OS predominante.
- **Qualidade**: taxa de solução (CLOSED / atribuídas) e tempo médio de
  resolução (`closed_at - opened_at`).
- **Retorno / recorrência**: revisitas — OS repetidas pro mesmo cliente+assunto
  dentro de uma janela curta. A revisita é atribuída ao técnico da OS *anterior*
  (cujo serviço motivou o retorno). Mede "retorno de credenciada" sem depender
  de uma flag de credenciada (que o IXC não expõe de forma confiável).
- **Score**: blend de solução (50%), ausência de retorno (30%) e produção (20%).

Mantida pura (sem ORM) pra ser testável isoladamente.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any


def compute_technician_stats(
    tickets: Iterable[Mapping[str, Any]],
    *,
    return_window_days: int = 30,
) -> list[dict[str, Any]]:
    """Agrega métricas por técnico e devolve o ranking (maior score primeiro).

    Cada item de `tickets` deve ter as chaves: `technician_id`,
    `customer_external_id`, `subject_id`, `status`, `opened_at`, `closed_at`.
    OS sem técnico atribuído são ignoradas na agregação por técnico (mas ainda
    participam da detecção de revisitas como "OS posterior").
    """
    rows = list(tickets)
    window = timedelta(days=return_window_days)

    # --- 1. Detecção de revisitas (mesmo cliente + assunto, janela curta) ---
    # Atribui a revisita ao técnico da OS anterior do par.
    returns_by_tech: Counter[str] = Counter()
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for t in rows:
        if t.get("opened_at") is None:
            continue
        key = (t.get("customer_external_id") or "", t.get("subject_id") or "")
        groups[key].append(t)

    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda t: t["opened_at"])
        for prev, nxt in pairwise(ordered):
            gap: datetime = nxt["opened_at"] - prev["opened_at"]
            if gap <= window:
                tech = (prev.get("technician_id") or "").strip()
                if tech:
                    returns_by_tech[tech] += 1

    # --- 2. Agregação por técnico ---
    total: Counter[str] = Counter()
    closed: Counter[str] = Counter()
    res_seconds: dict[str, float] = defaultdict(float)
    res_count: Counter[str] = Counter()
    subjects: dict[str, Counter[str]] = defaultdict(Counter)

    for t in rows:
        tech = (t.get("technician_id") or "").strip()
        if not tech:
            continue
        total[tech] += 1
        subjects[tech][(t.get("subject_id") or "").strip()] += 1
        if t.get("status") == "CLOSED":
            closed[tech] += 1
            opened_at = t.get("opened_at")
            closed_at = t.get("closed_at")
            if opened_at is not None and closed_at is not None:
                res_seconds[tech] += (closed_at - opened_at).total_seconds()
                res_count[tech] += 1

    max_total = max(total.values(), default=0)

    result: list[dict[str, Any]] = []
    for tech, tech_total in total.items():
        tech_closed = closed[tech]
        solution_rate = round(tech_closed / tech_total * 100, 1) if tech_total else 0.0
        avg_hours = (
            res_seconds[tech] / res_count[tech] / 3600 if res_count[tech] else 0.0
        )
        returns = returns_by_tech[tech]
        return_rate = round(returns / tech_total * 100, 1) if tech_total else 0.0
        volume_factor = (tech_total / max_total * 100) if max_total else 0.0
        score = round(
            solution_rate * 0.5 + (100 - return_rate) * 0.3 + volume_factor * 0.2,
            1,
        )
        top_subject_id = ""
        if subjects[tech]:
            top_subject_id = subjects[tech].most_common(1)[0][0]
        result.append({
            "technician_id": tech,
            "total": tech_total,
            "closed": tech_closed,
            "open": tech_total - tech_closed,
            "solution_rate": solution_rate,
            "avg_res_hours": avg_hours,
            "returns": returns,
            "return_rate": return_rate,
            "top_subject_id": top_subject_id,
            "score": score,
        })

    result.sort(key=lambda r: r["score"], reverse=True)
    return result
