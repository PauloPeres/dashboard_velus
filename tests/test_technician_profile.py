"""Testes de perfil (rua/interno) e evolução temporal de técnicos (#33).

Cobre `technician_profile`, a integração de perfil/categoria em
`compute_technician_stats` (com `subject_to_category`) e
`compute_technician_monthly` (produção mês a mês por técnico).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from apps.helpdesk.application.technician_stats import (
    PROFILE_FIELD,
    PROFILE_INTERNAL,
    compute_technician_monthly,
    compute_technician_stats,
    technician_profile,
)

NOW = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)

# subject_id → categoria. 10=SUPPORT (rua), 20=INSTALL (rua), 30=FINANCE (interno).
SUBJECTS = {"10": "SUPPORT", "20": "INSTALL", "30": "FINANCE", "40": "OTHER"}


def _t(
    *,
    tech: str,
    subject: str,
    opened_offset_days: float = 1,
    customer: str = "c1",
    status: str = "OPEN",
) -> dict[str, Any]:
    return {
        "technician_id": tech,
        "customer_external_id": customer,
        "subject_id": subject,
        "status": status,
        "opened_at": NOW - timedelta(days=opened_offset_days),
        "closed_at": None,
    }


class TestTechnicianProfile:
    def test_field_when_field_categories_dominate(self) -> None:
        assert technician_profile({"SUPPORT": 5, "FINANCE": 1}) == PROFILE_FIELD

    def test_internal_when_internal_dominate(self) -> None:
        assert technician_profile({"FINANCE": 5, "SUPPORT": 1}) == PROFILE_INTERNAL

    def test_tie_goes_to_field(self) -> None:
        assert technician_profile({"SUPPORT": 2, "FINANCE": 2}) == PROFILE_FIELD

    def test_other_is_neutral_and_no_signal_is_internal(self) -> None:
        assert technician_profile({"OTHER": 10}) == PROFILE_INTERNAL

    def test_empty_is_internal(self) -> None:
        assert technician_profile({}) == PROFILE_INTERNAL


class TestStatsWithCategory:
    def test_adds_profile_and_category_keys(self) -> None:
        tickets = [
            _t(tech="49", subject="10"),  # SUPPORT
            _t(tech="49", subject="20", customer="c2"),  # INSTALL
            _t(tech="50", subject="30"),  # FINANCE
        ]
        stats = {
            s["technician_id"]: s
            for s in compute_technician_stats(
                tickets, subject_to_category=SUBJECTS
            )
        }
        assert stats["49"]["profile"] == PROFILE_FIELD
        assert stats["49"]["profile_label"] == "Rua"
        assert stats["50"]["profile"] == PROFILE_INTERNAL
        assert stats["50"]["profile_label"] == "Interno"
        assert stats["49"]["category_counts"] == {"SUPPORT": 1, "INSTALL": 1}
        assert stats["49"]["top_category"] in {"SUPPORT", "INSTALL"}

    def test_no_category_keys_without_mapping(self) -> None:
        stats = compute_technician_stats([_t(tech="49", subject="10")])
        assert "profile" not in stats[0]
        assert "category_counts" not in stats[0]


class TestTechnicianMonthly:
    def test_counts_per_month_and_total(self) -> None:
        tickets = [
            _t(tech="49", subject="10", opened_offset_days=0),  # 02/06 — mês atual
            _t(tech="49", subject="10", opened_offset_days=1, customer="c2"),  # 01/06
            _t(tech="49", subject="10", opened_offset_days=10, customer="c3"),  # 23/05
        ]
        out = compute_technician_monthly(tickets, now=NOW, months=3)
        assert len(out["labels"]) == 3
        assert out["month_keys"][-1] == "2026-06"
        series = {s["technician_id"]: s for s in out["per_tech"]}
        assert series["49"]["total"] == 3
        # 2 no mês corrente (último bucket), 1 no mês anterior.
        assert series["49"]["values"][-1] == 2
        assert series["49"]["values"][-2] == 1

    def test_sorted_by_total_desc(self) -> None:
        tickets = [
            _t(tech="a", subject="10"),
            _t(tech="b", subject="10", customer="c2"),
            _t(tech="b", subject="10", customer="c3"),
        ]
        out = compute_technician_monthly(tickets, now=NOW, months=3)
        assert [s["technician_id"] for s in out["per_tech"]] == ["b", "a"]

    def test_skips_blank_tech_and_out_of_window(self) -> None:
        tickets = [
            _t(tech="", subject="10"),
            _t(tech="49", subject="10", opened_offset_days=400),  # fora da janela
        ]
        out = compute_technician_monthly(tickets, now=NOW, months=3)
        assert out["per_tech"] == []
