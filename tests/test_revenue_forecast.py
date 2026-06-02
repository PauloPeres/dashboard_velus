"""Testes do forecast financeiro melhorado (#25).

`compute_revenue_forecast` deixou de ser crescimento composto de 3 meses e passou
a usar tendência linear (OLS), sazonalidade amortecida por mês-do-ano, taxa de
recebimento com tendência e banda de cenários (otimista/pessimista). As chaves
consumidas por view/chart/template foram preservadas.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _first_of_month_n_ago,
    _ols_fit,
    _seasonal_factors,
    compute_revenue_forecast,
)
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0

# Chaves que view/chart/template dependem — não podem sumir.
_REQUIRED_KEYS = {
    "month",
    "label",
    "forecast_mrr",
    "forecast_mrr_optimistic",
    "forecast_mrr_pessimistic",
    "forecast_cash",
    "forecast_expenses",
    "forecast_net",
    "forecast_net_optimistic",
    "forecast_net_pessimistic",
    "seasonal_factor",
    "collection_rate_pct",
    "is_forecast",
}


def _sample_date_for(today: date, i: int) -> date:
    """Replica a data amostrada por `compute_mrr_series` para o mês `i` atrás."""
    if i > 0:
        return _first_of_month_n_ago(today, i - 1) - timedelta(days=1)
    return today


def _make_mrr_history(org: Organization, monthly_values: list[Decimal]) -> None:
    """Cria snapshots de contrato para os últimos N meses (mais antigo→atual).

    Um contrato por mês com `monthly_amount` = valor, snapshot na data que o
    `compute_mrr_series` amostra — assim MRR[mês] = valor.
    """
    global _seq
    set_current_organization(org)
    today = timezone.now().date()
    n = len(monthly_values)
    for idx, value in enumerate(monthly_values):
        months_ago = n - 1 - idx
        _seq += 1
        contract = Contract.objects.create(
            organization=org,
            source_type="FAKE",
            external_id=f"fc-ctr-{_seq}",
            customer_external_id=f"fc-cust-{_seq}",
            plan_name="Plano X",
            monthly_amount=value,
            status="ACTIVE",
        )
        FactContractStatusDaily.objects.create(
            organization=org,
            contract=contract,
            date=_sample_date_for(today, months_ago),
            status="ACTIVE",
            monthly_amount=value,
            is_active=True,
        )


class TestOlsFit:
    def test_empty_and_single(self) -> None:
        assert _ols_fit([]) == (0.0, 0.0)
        assert _ols_fit([5.0]) == (0.0, 5.0)

    def test_perfect_linear_trend(self) -> None:
        slope, intercept = _ols_fit([10.0, 20.0, 30.0, 40.0])
        assert slope == pytest.approx(10.0)
        assert intercept == pytest.approx(10.0)

    def test_flat_series_zero_slope(self) -> None:
        slope, intercept = _ols_fit([7.0, 7.0, 7.0])
        assert slope == pytest.approx(0.0)
        assert intercept == pytest.approx(7.0)


class TestSeasonalFactors:
    def test_damping_pulls_toward_one(self) -> None:
        # Observado = 2x a tendência no mês 3 → razão 2.0, damping 0.5 → 1.5.
        factors = _seasonal_factors([3], [200.0], [100.0], damping=0.5)
        assert factors[3] == pytest.approx(1.5)

    def test_zero_damping_disables_seasonality(self) -> None:
        factors = _seasonal_factors([1, 2], [200.0, 50.0], [100.0, 100.0], damping=0.0)
        assert factors[1] == pytest.approx(1.0)
        assert factors[2] == pytest.approx(1.0)

    def test_averages_repeated_month(self) -> None:
        # Mesmo mês-do-ano duas vezes: razões 2.0 e 0.0 → média 1.0 → fator 1.0.
        factors = _seasonal_factors([6, 6], [200.0, 0.0], [100.0, 100.0], damping=1.0)
        assert factors[6] == pytest.approx(1.0)

    def test_skips_nonpositive_trend(self) -> None:
        factors = _seasonal_factors([4], [50.0], [0.0])
        assert 4 not in factors


@pytest.mark.django_db
class TestComputeRevenueForecast:
    def test_emits_required_keys_and_horizon(
        self, organization_a: Organization
    ) -> None:
        _make_mrr_history(
            organization_a,
            [Decimal(str(v)) for v in (100, 110, 120, 130, 140, 150)],
        )
        set_current_organization(organization_a)

        result = compute_revenue_forecast(organization_a, months_ahead=12)
        assert len(result) == 12
        for row in result:
            assert _REQUIRED_KEYS <= set(row)
            assert row["is_forecast"] is True

    def test_months_contiguous_and_future(
        self, organization_a: Organization
    ) -> None:
        _make_mrr_history(
            organization_a,
            [Decimal(str(v)) for v in (100, 110, 120, 130, 140, 150)],
        )
        set_current_organization(organization_a)

        result = compute_revenue_forecast(organization_a, months_ahead=6)
        months = [r["month"] for r in result]
        assert months == sorted(months)
        today_key = timezone.now().date().strftime("%Y-%m")
        assert all(m > today_key for m in months)

    def test_scenario_band_ordering(self, organization_a: Organization) -> None:
        # Histórico com ruído → resíduos não nulos → banda abre.
        _make_mrr_history(
            organization_a,
            [Decimal(str(v)) for v in (100, 130, 105, 160, 120, 180)],
        )
        set_current_organization(organization_a)

        result = compute_revenue_forecast(organization_a, months_ahead=12)
        for row in result:
            assert row["forecast_mrr_pessimistic"] <= row["forecast_mrr"]
            assert row["forecast_mrr"] <= row["forecast_mrr_optimistic"]
        # Banda alarga com o horizonte: spread do último > spread do primeiro.
        first = result[0]["forecast_mrr_optimistic"] - result[0]["forecast_mrr_pessimistic"]
        last = result[-1]["forecast_mrr_optimistic"] - result[-1]["forecast_mrr_pessimistic"]
        assert last > first

    def test_collection_rate_within_bounds(
        self, organization_a: Organization
    ) -> None:
        _make_mrr_history(
            organization_a,
            [Decimal(str(v)) for v in (100, 110, 120, 130, 140, 150)],
        )
        set_current_organization(organization_a)

        result = compute_revenue_forecast(organization_a, months_ahead=3)
        for row in result:
            assert 50.0 <= row["collection_rate_pct"] <= 105.0

    def test_naive_fallback_for_short_history(
        self, organization_a: Organization
    ) -> None:
        # Só 2 meses de MRR (< 4) → cai no modelo ingênuo, mas ainda emite chaves.
        _make_mrr_history(organization_a, [Decimal("100"), Decimal("110")])
        set_current_organization(organization_a)

        result = compute_revenue_forecast(organization_a, months_ahead=6)
        assert len(result) == 6
        for row in result:
            assert _REQUIRED_KEYS <= set(row)
            # Sem cenário no fallback: banda colapsa no valor base.
            assert row["forecast_mrr_optimistic"] == row["forecast_mrr"]
            assert row["forecast_mrr_pessimistic"] == row["forecast_mrr"]
            assert row["seasonal_factor"] == 1.0
