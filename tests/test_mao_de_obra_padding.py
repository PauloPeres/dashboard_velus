"""Testes do padding de 12 meses no detalhamento de Mão de Obra (#24).

`compute_mao_de_obra_detail` só retornava meses com lançamento — meses sem
despesa sumiam do eixo do gráfico. Agora a janela é preenchida (cutoff → mês
atual) com 0,0 nos meses zerados.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _MAO_DE_OBRA_NAME,
    _full_month_keys,
    compute_mao_de_obra_detail,
)
from apps.financial.infrastructure.models import Expense
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_mao_expense(org: Organization, *, paid_at: date, amount: Decimal) -> Expense:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Expense.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"mao-{_seq}",
        supplier_external_id="999",
        supplier_name=_MAO_DE_OBRA_NAME,
        amount=amount,
        due_date=paid_at,
        paid_at=paid_at,
        status="PAID",
        description="EQUIPE A",
    )


class TestFullMonthKeys:
    def test_contiguous_range_inclusive(self) -> None:
        keys = _full_month_keys(date(2025, 11, 1), date(2026, 2, 15))
        assert keys == ["2025-11", "2025-12", "2026-01", "2026-02"]

    def test_single_month(self) -> None:
        assert _full_month_keys(date(2026, 6, 1), date(2026, 6, 30)) == ["2026-06"]


@pytest.mark.django_db
class TestMaoDeObraPadding:
    def test_fills_full_window_even_with_one_month(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        _make_mao_expense(
            organization_a, paid_at=today.replace(day=15), amount=Decimal("1000")
        )
        set_current_organization(organization_a)

        result = compute_mao_de_obra_detail(organization_a, months=12)
        # Janela contígua de 13 chaves (cutoff 12m atrás → mês atual inclusive).
        assert len(result["months"]) >= 12
        assert len(result["month_labels"]) == len(result["months"])
        # Sem buracos: meses estritamente crescentes e contíguos.
        assert result["months"] == sorted(result["months"])

    def test_zero_months_present_in_category_series(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        this_month = today.replace(day=15)
        _make_mao_expense(organization_a, paid_at=this_month, amount=Decimal("500"))
        set_current_organization(organization_a)

        result = compute_mao_de_obra_detail(organization_a, months=12)
        cat = result["by_category"][0]
        # A série mensal cobre toda a janela e tem zeros nos meses sem lançamento.
        assert len(cat["monthly"]) == len(result["months"])
        assert any(v == 0.0 for v in cat["monthly"])
        assert sum(cat["monthly"]) == 500.0

    def test_current_month_value_is_present(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        _make_mao_expense(
            organization_a, paid_at=today.replace(day=10), amount=Decimal("750")
        )
        set_current_organization(organization_a)

        result = compute_mao_de_obra_detail(organization_a, months=12)
        cur_key = today.strftime("%Y-%m")
        assert cur_key in result["months"]
        idx = result["months"].index(cur_key)
        assert result["by_category"][0]["monthly"][idx] == 750.0
