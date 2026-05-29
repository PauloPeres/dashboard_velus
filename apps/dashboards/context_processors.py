"""Context processor: injeta período selecionado em todos os templates."""
from __future__ import annotations
from typing import Any
from django.http import HttpRequest

_VALID = (3, 6, 12, 24)


def period_context(request: HttpRequest) -> dict[str, Any]:
    try:
        months = int(request.GET.get("months", 12))
        if months not in _VALID:
            months = 12
    except (ValueError, TypeError):
        months = 12
    return {
        "selected_months": months,
        "period_options": [
            {"value": 3,  "label": "3m"},
            {"value": 6,  "label": "6m"},
            {"value": 12, "label": "12m"},
            {"value": 24, "label": "24m"},
        ],
    }
