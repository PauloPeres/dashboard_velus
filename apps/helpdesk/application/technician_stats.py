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
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from apps.helpdesk.application.os_classification import (
    EQUIPMENT,
    INSTALL,
    SUPPORT,
    category_label,
)

# Perfil do técnico inferido pelos tipos de OS que ele atende. Categorias "de
# rua" exigem deslocamento até o cliente (visita técnica/instalação/equipamento);
# as demais são atendimento interno (financeiro/comercial/cadastro/retenção).
PROFILE_FIELD = "FIELD"
PROFILE_INTERNAL = "INTERNAL"
PROFILE_LABELS = {PROFILE_FIELD: "Rua", PROFILE_INTERNAL: "Interno"}
_FIELD_CATEGORIES = frozenset({SUPPORT, INSTALL, EQUIPMENT})


def profile_label(profile: str) -> str:
    """Rótulo legível do perfil do técnico (Rua/Interno)."""
    return PROFILE_LABELS.get(profile, profile)


def technician_profile(category_counts: Mapping[str, int]) -> str:
    """Infere o perfil (rua/interno) pela predominância de categorias de OS.

    Conta OS em categorias "de rua" (SUPPORT/INSTALL/EQUIPMENT) vs o restante
    classificado (financeiro/comercial/admin/lifecycle). Empate ou maioria de
    rua → Rua; maioria interna ou ausência total de sinal → Interno. OTHER é
    neutro (não conta pra nenhum lado).
    """
    field = sum(c for cat, c in category_counts.items() if cat in _FIELD_CATEGORIES)
    internal = sum(
        c
        for cat, c in category_counts.items()
        if cat not in _FIELD_CATEGORIES and cat != "OTHER"
    )
    if field == 0 and internal == 0:
        return PROFILE_INTERNAL
    return PROFILE_FIELD if field >= internal else PROFILE_INTERNAL


def compute_technician_stats(
    tickets: Iterable[Mapping[str, Any]],
    *,
    return_window_days: int = 30,
    subject_to_category: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Agrega métricas por técnico e devolve o ranking (maior score primeiro).

    Cada item de `tickets` deve ter as chaves: `technician_id`,
    `customer_external_id`, `subject_id`, `status`, `opened_at`, `closed_at`.
    OS sem técnico atribuído são ignoradas na agregação por técnico (mas ainda
    participam da detecção de revisitas como "OS posterior").

    Quando `subject_to_category` é fornecido (mapa `subject_id → categoria`),
    cada linha ganha `category_counts`, `top_category`/`top_category_label` e o
    `profile`/`profile_label` (rua/interno) inferido pelos tipos atendidos.
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
    categories: dict[str, Counter[str]] = defaultdict(Counter)

    for t in rows:
        tech = (t.get("technician_id") or "").strip()
        if not tech:
            continue
        total[tech] += 1
        subject_id = (t.get("subject_id") or "").strip()
        subjects[tech][subject_id] += 1
        if subject_to_category is not None:
            categories[tech][subject_to_category.get(subject_id, "OTHER")] += 1
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
        row: dict[str, Any] = {
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
        }
        if subject_to_category is not None:
            cat_counts = dict(categories[tech])
            top_category = (
                categories[tech].most_common(1)[0][0] if categories[tech] else "OTHER"
            )
            profile = technician_profile(cat_counts)
            row.update(
                category_counts=cat_counts,
                top_category=top_category,
                top_category_label=category_label(top_category),
                profile=profile,
                profile_label=profile_label(profile),
            )
        result.append(row)

    result.sort(key=lambda r: r["score"], reverse=True)
    return result


def _recent_months(now: datetime, months: int) -> list[tuple[int, int]]:
    """Lista de (ano, mês) dos últimos `months` meses, do mais antigo ao atual."""
    y, m = now.year, now.month
    out: list[tuple[int, int]] = []
    for _ in range(months):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    out.reverse()
    return out


def compute_technician_monthly(
    tickets: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    months: int = 6,
) -> dict[str, Any]:
    """Produção (OS abertas) por técnico, mês a mês — pra evolução temporal.

    Devolve `labels`/`month_keys` (eixo do tempo, mais antigo→atual) e `per_tech`:
    uma série por técnico com a contagem de OS de cada mês e o total no período,
    ordenada por total (maior primeiro). OS sem técnico ou sem `opened_at`, ou
    fora da janela, são ignoradas.
    """
    month_tuples = _recent_months(now, months)
    index = {ym: i for i, ym in enumerate(month_tuples)}
    labels = [date(y, m, 1).strftime("%b/%y") for y, m in month_tuples]
    month_keys = [f"{y:04d}-{m:02d}" for y, m in month_tuples]

    per_tech_counts: dict[str, list[int]] = defaultdict(lambda: [0] * months)
    for t in tickets:
        tech = (t.get("technician_id") or "").strip()
        opened_at = t.get("opened_at")
        if not tech or opened_at is None:
            continue
        i = index.get((opened_at.year, opened_at.month))
        if i is None:
            continue
        per_tech_counts[tech][i] += 1

    per_tech = [
        {"technician_id": tech, "values": values, "total": sum(values)}
        for tech, values in per_tech_counts.items()
    ]
    per_tech.sort(key=lambda r: r["total"], reverse=True)

    return {"labels": labels, "month_keys": month_keys, "per_tech": per_tech}
