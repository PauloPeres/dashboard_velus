"""Testes do SLA por tipo de atendimento (#34).

`compute_sla_by_category` é pura: recebe OS materializadas + mapa
`subject_id → categoria` e devolve um card por categoria com tempo médio de
atendimento (abertura→agendamento) e resolução (abertura→fechamento), além do
comparativo vs o período anterior (mesma duração).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.helpdesk.application.sla import compute_sla_by_category

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)

# subject_id → categoria (como sairia de classify_subject sobre o subject_map).
SUBJECTS = {"10": "SUPPORT", "20": "INSTALL"}


def _ticket(
    *,
    subject_id: str,
    opened_offset_days: float,
    status: str = "CLOSED",
    resolution_hours: float | None = None,
    response_hours: float | None = None,
) -> dict:
    """OS com abertura a `opened_offset_days` atrás de NOW."""
    opened_at = NOW - timedelta(days=opened_offset_days)
    closed_at = (
        opened_at + timedelta(hours=resolution_hours)
        if resolution_hours is not None
        else None
    )
    scheduled_at = (
        opened_at + timedelta(hours=response_hours)
        if response_hours is not None
        else None
    )
    return {
        "subject_id": subject_id,
        "status": status,
        "opened_at": opened_at,
        "scheduled_at": scheduled_at,
        "closed_at": closed_at,
    }


def _by_cat(rows: list[dict]) -> dict[str, dict]:
    return {r["category"]: r for r in rows}


def test_groups_by_category_and_labels() -> None:
    tickets = [
        _ticket(subject_id="10", opened_offset_days=5, resolution_hours=10),
        _ticket(subject_id="20", opened_offset_days=5, resolution_hours=48),
    ]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW))
    assert rows["SUPPORT"]["label"] == "Manutenção"
    assert rows["INSTALL"]["label"] == "Instalação"


def test_avg_resolution_hours() -> None:
    tickets = [
        _ticket(subject_id="10", opened_offset_days=5, resolution_hours=10),
        _ticket(subject_id="10", opened_offset_days=6, resolution_hours=20),
    ]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW))
    assert rows["SUPPORT"]["avg_resolution_hours"] == 15.0
    assert rows["SUPPORT"]["resolved_count"] == 2


def test_avg_response_uses_scheduled_at() -> None:
    tickets = [
        _ticket(
            subject_id="20",
            opened_offset_days=5,
            status="SCHEDULED",
            response_hours=72,
        ),
    ]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW))
    assert rows["INSTALL"]["avg_response_hours"] == 72.0
    assert rows["INSTALL"]["responded_count"] == 1
    # Não fechou → sem resolução.
    assert rows["INSTALL"]["resolved_count"] == 0


def test_only_closed_count_for_resolution() -> None:
    tickets = [
        _ticket(subject_id="10", opened_offset_days=5, status="OPEN"),
        _ticket(subject_id="10", opened_offset_days=5, resolution_hours=8),
    ]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW))
    assert rows["SUPPORT"]["count"] == 2
    assert rows["SUPPORT"]["resolved_count"] == 1
    assert rows["SUPPORT"]["avg_resolution_hours"] == 8.0


def test_comparative_delta_and_improvement() -> None:
    tickets = [
        # Período atual (dentro de 30 dias): 10h.
        _ticket(subject_id="10", opened_offset_days=5, resolution_hours=10),
        # Período anterior (30–60 dias): 20h → melhorou 10h (delta -10).
        _ticket(subject_id="10", opened_offset_days=40, resolution_hours=20),
    ]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW, period_days=30))
    s = rows["SUPPORT"]
    assert s["prev_avg_resolution_hours"] == 20.0
    assert s["resolution_delta_hours"] == -10.0
    assert s["resolution_improved"] is True


def test_delta_none_without_previous_base() -> None:
    tickets = [_ticket(subject_id="10", opened_offset_days=5, resolution_hours=10)]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW))
    s = rows["SUPPORT"]
    assert s["resolution_delta_hours"] is None
    assert s["resolution_improved"] is False


def test_unmapped_subject_falls_to_other() -> None:
    tickets = [_ticket(subject_id="999", opened_offset_days=5, resolution_hours=5)]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW))
    assert "OTHER" in rows
    assert rows["OTHER"]["label"] == "Outros"


def test_tickets_outside_two_windows_ignored() -> None:
    tickets = [
        _ticket(subject_id="10", opened_offset_days=5, resolution_hours=10),
        # Muito antigo (>60 dias) → fora das duas janelas.
        _ticket(subject_id="10", opened_offset_days=90, resolution_hours=99),
    ]
    rows = _by_cat(compute_sla_by_category(tickets, SUBJECTS, now=NOW, period_days=30))
    assert rows["SUPPORT"]["count"] == 1


def test_ticket_without_opened_at_skipped() -> None:
    tickets = [
        {
            "subject_id": "10",
            "status": "CLOSED",
            "opened_at": None,
            "scheduled_at": None,
            "closed_at": NOW,
        }
    ]
    assert compute_sla_by_category(tickets, SUBJECTS, now=NOW) == []


def test_sorted_by_volume_desc() -> None:
    tickets = [
        _ticket(subject_id="10", opened_offset_days=5, resolution_hours=1),
        _ticket(subject_id="20", opened_offset_days=5, resolution_hours=1),
        _ticket(subject_id="20", opened_offset_days=6, resolution_hours=1),
    ]
    rows = compute_sla_by_category(tickets, SUBJECTS, now=NOW)
    assert [r["category"] for r in rows] == ["INSTALL", "SUPPORT"]
