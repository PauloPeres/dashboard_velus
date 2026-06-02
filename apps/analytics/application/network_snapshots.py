"""Histórico temporal de rede — captura e leitura de snapshots (#35).

`Connection` só guarda o estado atual (o sync sobrescreve a cada ciclo), então
não há como ver tendência de conexões/banda. Aqui:

- `summarize_connections` (puro): agrega uma foto de conexões em contagens por
  status + banda acumulada e uptime.
- `capture_network_snapshot`: persiste essa foto numa linha de
  `FactNetworkSnapshot` (append-only) — chamado periodicamente pelo Beat.
- `build_network_history` (puro): transforma a lista de snapshots numa série
  temporal pronta pro gráfico.
- `compute_network_history`: lê os snapshots da janela e delega.

As funções puras (sem ORM) concentram a lógica pra ficarem testáveis sem banco.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

_ONLINE = "ONLINE"
_OFFLINE = "OFFLINE"
_BLOCKED = "BLOCKED"


def summarize_connections(connections: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Agrega conexões em contagens por status + banda acumulada (função pura).

    Cada item precisa de `status`; `rx_bytes`/`tx_bytes` são opcionais (None→0).
    Uptime = online / (online + offline); bloqueados não contam como falha.
    """
    online = offline = blocked = unknown = 0
    rx_total = tx_total = 0
    for c in connections:
        status = c["status"]
        if status == _ONLINE:
            online += 1
        elif status == _OFFLINE:
            offline += 1
        elif status == _BLOCKED:
            blocked += 1
        else:
            unknown += 1
        rx_total += c.get("rx_bytes") or 0
        tx_total += c.get("tx_bytes") or 0

    active_base = online + offline
    uptime_pct = round(online / active_base * 100, 1) if active_base else 0.0
    return {
        "total_count": online + offline + blocked + unknown,
        "online_count": online,
        "offline_count": offline,
        "blocked_count": blocked,
        "unknown_count": unknown,
        "uptime_pct": uptime_pct,
        "rx_bytes_total": rx_total,
        "tx_bytes_total": tx_total,
    }


def build_network_history(
    snapshots: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Monta a série temporal pro gráfico a partir de snapshots (função pura).

    `snapshots` deve vir ordenado por `captured_at` ascendente. Cada item tem
    `captured_at` (datetime), `online_count`, `offline_count`, `uptime_pct`,
    `rx_bytes_total`, `tx_bytes_total`.
    """
    labels: list[str] = []
    online: list[int] = []
    offline: list[int] = []
    uptime: list[float] = []
    bandwidth_gb: list[float] = []
    for s in snapshots:
        labels.append(_format_label(s["captured_at"]))
        online.append(s["online_count"])
        offline.append(s["offline_count"])
        uptime.append(float(s["uptime_pct"]))
        total_bytes = (s["rx_bytes_total"] or 0) + (s["tx_bytes_total"] or 0)
        bandwidth_gb.append(round(total_bytes / 1024**3, 2))
    return {
        "labels": labels,
        "online": online,
        "offline": offline,
        "uptime": uptime,
        "bandwidth_gb": bandwidth_gb,
        "count": len(labels),
    }


def _format_label(value: datetime) -> str:
    return timezone.localtime(value).strftime("%d/%m %Hh")


# =============================================================================
# Wrappers com ORM
# =============================================================================
def capture_network_snapshot(organization: Any) -> Any:
    """Lê o estado atual das conexões e grava 1 snapshot (append-only)."""
    from apps.analytics.infrastructure.models import FactNetworkSnapshot
    from apps.network.infrastructure.models import Connection

    rows = Connection.objects.filter(organization=organization).values(
        "status", "rx_bytes", "tx_bytes"
    )
    metrics = summarize_connections(rows)
    return FactNetworkSnapshot.objects.create(
        organization=organization,
        captured_at=timezone.now(),
        **metrics,
    )


def compute_network_history(organization: Any, *, days: int = 30) -> dict[str, Any]:
    """Lê os snapshots da janela (`days`) e devolve a série temporal."""
    from apps.analytics.infrastructure.models import FactNetworkSnapshot

    cutoff = timezone.now() - timedelta(days=days)
    snapshots = list(
        FactNetworkSnapshot.objects.filter(
            organization=organization, captured_at__gte=cutoff
        )
        .order_by("captured_at")
        .values(
            "captured_at",
            "online_count",
            "offline_count",
            "uptime_pct",
            "rx_bytes_total",
            "tx_bytes_total",
        )
    )
    return build_network_history(snapshots)
