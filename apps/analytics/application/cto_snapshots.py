"""Histórico temporal de CTOs FTTH — captura e leitura de snapshots.

Padrão idêntico ao network_snapshots: a cada captura (Beat) grava 1 linha
de FactCtoSnapshot com o estado agregado das caixas de distribuição,
permitindo visualizar crescimento de portas ocupadas/livres ao longo do tempo.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from django.utils import timezone


def build_cto_history(
    snapshots: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Monta a série temporal pro gráfico a partir de snapshots (função pura).

    Agrupa por mês — usa o último snapshot de cada mês para representar o
    estado no final daquele período.
    """
    if not snapshots:
        return {"labels": [], "total_ctos": [], "occupied": [], "free": [], "occupancy_pct": [], "count": 0}

    # Agrupar por mês (YYYY-MM) — manter último snapshot de cada mês
    by_month: dict[str, dict[str, Any]] = {}
    for s in snapshots:
        cap = s["captured_at"]
        key = timezone.localtime(cap).strftime("%Y-%m")
        by_month[key] = s  # último ganha (snapshots vêm ordenados asc)

    labels: list[str] = []
    total_ctos: list[int] = []
    occupied: list[int] = []
    free: list[int] = []
    occupancy_pct: list[float] = []

    for key in sorted(by_month):
        s = by_month[key]
        cap = s["captured_at"]
        labels.append(timezone.localtime(cap).strftime("%b/%y"))
        total_ctos.append(s["total_ctos"])
        occupied.append(s["occupied_ports"])
        free.append(s["free_ports"])
        occupancy_pct.append(float(s["occupancy_pct"]))

    return {
        "labels": labels,
        "total_ctos": total_ctos,
        "occupied": occupied,
        "free": free,
        "occupancy_pct": occupancy_pct,
        "count": len(labels),
    }


# =============================================================================
# Wrappers com ORM
# =============================================================================

def capture_cto_snapshot(organization: Any) -> Any:
    """Captura o estado atual das CTOs e grava 1 snapshot (append-only)."""
    from apps.analytics.application.aggregations import compute_cto_summary
    from apps.analytics.infrastructure.models import FactCtoSnapshot

    summary = compute_cto_summary(organization)
    return FactCtoSnapshot.objects.create(
        organization=organization,
        captured_at=timezone.now(),
        total_ctos=summary["total_ctos"],
        total_ports=summary["total_ports"],
        occupied_ports=summary["total_occupied"],
        free_ports=summary["total_free"],
        occupancy_pct=summary["occupancy_pct"],
        by_project=summary["by_project"],
    )


def compute_cto_history(organization: Any, *, months: int = 12) -> dict[str, Any]:
    """Lê os snapshots da janela e devolve a série temporal mensal."""
    from apps.analytics.infrastructure.models import FactCtoSnapshot

    cutoff = timezone.now() - timedelta(days=months * 31)
    snapshots = list(
        FactCtoSnapshot.objects.filter(
            organization=organization, captured_at__gte=cutoff
        )
        .order_by("captured_at")
        .values(
            "captured_at",
            "total_ctos",
            "total_ports",
            "occupied_ports",
            "free_ports",
            "occupancy_pct",
        )
    )
    return build_cto_history(snapshots)
