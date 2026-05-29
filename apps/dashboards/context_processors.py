"""Context processor: injeta período selecionado em todos os templates."""
from __future__ import annotations

from datetime import date
from typing import Any

from django.http import HttpRequest

_VALID = (1, 2, 3, 6, 12, 24)

_PT_MONTHS = ["", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
              "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def _month_label(year: int, month: int) -> str:
    return f"{_PT_MONTHS[month]}/{year}"


def period_context(request: HttpRequest) -> dict[str, Any]:
    today = date.today()

    cur_label = _month_label(today.year, today.month)
    last_m = today.month - 1 if today.month > 1 else 12
    last_y = today.year if today.month > 1 else today.year - 1
    last_label = _month_label(last_y, last_m)

    try:
        months = int(request.GET.get("months", 12))
        if months not in _VALID:
            months = 12
    except (ValueError, TypeError):
        months = 12

    return {
        "selected_months": months,
        "period_options": [
            {"value": 1,  "label": f"Mês Atual ({cur_label})"},
            {"value": 2,  "label": f"Último Mês ({last_label})"},
            {"value": 3,  "label": "3 meses"},
            {"value": 6,  "label": "6 meses"},
            {"value": 12, "label": "12 meses"},
            {"value": 24, "label": "24 meses"},
        ],
    }
