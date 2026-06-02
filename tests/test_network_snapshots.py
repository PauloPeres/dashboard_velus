"""Testes do histórico temporal de rede (#35).

Cobre as funções puras `summarize_connections` (agregação de uma foto de
conexões) e `build_network_history` (montagem da série temporal a partir de
snapshots). Sem ORM — lógica isolada e testável sem banco.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.analytics.application.network_snapshots import (
    build_network_history,
    summarize_connections,
)


def _conn(status: str, rx: int = 0, tx: int = 0) -> dict[str, Any]:
    return {"status": status, "rx_bytes": rx, "tx_bytes": tx}


class TestSummarizeConnections:
    def test_counts_by_status(self) -> None:
        out = summarize_connections([
            _conn("ONLINE"),
            _conn("ONLINE"),
            _conn("OFFLINE"),
            _conn("BLOCKED"),
            _conn("UNKNOWN"),
        ])
        assert out["total_count"] == 5
        assert out["online_count"] == 2
        assert out["offline_count"] == 1
        assert out["blocked_count"] == 1
        assert out["unknown_count"] == 1

    def test_unknown_status_falls_into_unknown(self) -> None:
        out = summarize_connections([_conn("WEIRD"), _conn("")])
        assert out["unknown_count"] == 2
        assert out["online_count"] == 0

    def test_uptime_ignores_blocked(self) -> None:
        # 3 online, 1 offline, 4 blocked → uptime = 3/4 = 75% (bloqueado não conta)
        conns = (
            [_conn("ONLINE")] * 3 + [_conn("OFFLINE")] + [_conn("BLOCKED")] * 4
        )
        out = summarize_connections(conns)
        assert out["uptime_pct"] == 75.0

    def test_uptime_zero_when_no_active_base(self) -> None:
        out = summarize_connections([_conn("BLOCKED"), _conn("UNKNOWN")])
        assert out["uptime_pct"] == 0.0

    def test_accumulates_bandwidth_and_handles_none(self) -> None:
        out = summarize_connections([
            {"status": "ONLINE", "rx_bytes": 100, "tx_bytes": 50},
            {"status": "ONLINE", "rx_bytes": None, "tx_bytes": None},
            {"status": "OFFLINE"},  # sem chaves de banda
        ])
        assert out["rx_bytes_total"] == 100
        assert out["tx_bytes_total"] == 50

    def test_empty_is_all_zero(self) -> None:
        out = summarize_connections([])
        assert out["total_count"] == 0
        assert out["uptime_pct"] == 0.0
        assert out["rx_bytes_total"] == 0


class TestBuildNetworkHistory:
    def _snap(self, day: int, online: int, offline: int, rx: int, tx: int) -> dict[str, Any]:
        return {
            "captured_at": datetime(2026, 6, day, 12, 0, tzinfo=UTC),
            "online_count": online,
            "offline_count": offline,
            "uptime_pct": 90.0,
            "rx_bytes_total": rx,
            "tx_bytes_total": tx,
        }

    def test_series_aligned_with_snapshots(self) -> None:
        out = build_network_history([
            self._snap(1, 10, 2, 0, 0),
            self._snap(2, 12, 1, 0, 0),
        ])
        assert out["count"] == 2
        assert out["online"] == [10, 12]
        assert out["offline"] == [2, 1]
        assert len(out["labels"]) == 2

    def test_bandwidth_converted_to_gb(self) -> None:
        gb = 1024**3
        out = build_network_history([self._snap(1, 1, 0, 2 * gb, gb)])
        # (2 + 1) GB = 3.0
        assert out["bandwidth_gb"] == [3.0]

    def test_uptime_cast_to_float(self) -> None:
        out = build_network_history([self._snap(1, 1, 0, 0, 0)])
        assert out["uptime"] == [90.0]
        assert isinstance(out["uptime"][0], float)

    def test_empty_returns_empty_series(self) -> None:
        out = build_network_history([])
        assert out["count"] == 0
        assert out["labels"] == []
        assert out["online"] == []
