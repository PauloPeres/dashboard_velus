"""Testes das séries temporais de /contracts/ (#42).

Cobre o helper puro `_cumulative_equipment_by_month` (parque acumulado por mês)
e a integração `compute_contract_kpi_trend` (ARPU + churn % por mês a partir de
FactContractStatusDaily + Contract.canceled_at) e `compute_equipment_field_trend`
(parque ativo acumulado pela data real de instalação em raw_extras).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _cumulative_equipment_by_month,
    compute_contract_kpi_trend,
    compute_equipment_field_trend,
    compute_kpis,
)
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.inventory.infrastructure.models import ContractEquipment
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


class TestCumulativeEquipmentByMonth:
    def test_counts_are_cumulative(self) -> None:
        today = date(2026, 6, 15)
        dates = [date(2026, 4, 10), date(2026, 5, 20), date(2026, 6, 1)]
        out = _cumulative_equipment_by_month(dates, today=today, months=3)
        assert [r["count"] for r in out] == [1, 2, 3]

    def test_future_dates_ignored(self) -> None:
        today = date(2026, 6, 15)
        dates = [date(2026, 4, 10), date(2030, 1, 1)]
        out = _cumulative_equipment_by_month(dates, today=today, months=3)
        assert out[-1]["count"] == 1

    def test_empty_yields_zeros(self) -> None:
        today = date(2026, 6, 15)
        out = _cumulative_equipment_by_month([], today=today, months=3)
        assert [r["count"] for r in out] == [0, 0, 0]
        assert len(out) == 3

    def test_labels_and_length(self) -> None:
        today = date(2026, 6, 15)
        out = _cumulative_equipment_by_month([], today=today, months=6)
        assert len(out) == 6
        assert all("label" in r and "month" in r for r in out)


def _aware(d: date) -> datetime:
    return timezone.make_aware(datetime.combine(d, time(12, 0)))


def _active_snapshot(org: Organization, *, on: date, monthly: Decimal) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    contract = Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"kpi-ctr-{_seq}",
        customer_external_id=f"kpi-cust-{_seq}",
        plan_name="Plano X",
        monthly_amount=monthly,
        status="ACTIVE",
    )
    FactContractStatusDaily.objects.create(
        organization=org,
        contract=contract,
        date=on,
        status="ACTIVE",
        monthly_amount=monthly,
        is_active=True,
    )


@pytest.mark.django_db
class TestContractKpiTrend:
    def test_arpu_is_mrr_over_active(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        _active_snapshot(organization_a, on=today, monthly=Decimal("100"))
        _active_snapshot(organization_a, on=today, monthly=Decimal("200"))

        trend = compute_contract_kpi_trend(organization_a, months=3)
        current = trend[-1]
        assert current["active"] == 2
        assert current["mrr"] == 300.0
        assert current["arpu"] == 150.0

    def test_churn_counts_canceled_in_month(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        month_first = today.replace(day=1)
        # base ativa no início do mês: 1 contrato no último dia do mês anterior
        _active_snapshot(
            organization_a, on=month_first - timedelta(days=1), monthly=Decimal("100")
        )
        set_current_organization(organization_a)
        global _seq
        _seq += 1
        Contract.objects.create(
            organization=organization_a,
            source_type="FAKE",
            external_id=f"kpi-cancel-{_seq}",
            customer_external_id=f"kpi-ccust-{_seq}",
            plan_name="Plano X",
            monthly_amount=Decimal("100"),
            status="CANCELED",
            canceled_at=_aware(today),
        )

        trend = compute_contract_kpi_trend(organization_a, months=3)
        current = trend[-1]
        assert current["canceled"] == 1
        assert current["churn_pct"] == 100.0

    def test_empty_org_is_graceful(self, organization_a: Organization) -> None:
        trend = compute_contract_kpi_trend(organization_a, months=4)
        assert len(trend) == 4
        assert all(r["arpu"] == 0.0 and r["churn_pct"] == 0.0 for r in trend)


def _make_equipment(org: Organization, *, status: str, data: str | None) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    ContractEquipment.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"eq-{_seq}",
        contract_external_id=f"eq-ctr-{_seq}",
        product_name="ONT",
        status=status,
        raw_extras={"data": data} if data is not None else {},
    )


@pytest.mark.django_db
class TestComputeKpisChurnClosedMonth:
    def test_churn_uses_last_closed_month_not_partial(
        self, organization_a: Organization
    ) -> None:
        today = timezone.now().date()
        month_first = today.replace(day=1)
        last_month_first = (month_first - timedelta(days=1)).replace(day=1)

        # Base ativa no início do mês fechado (anterior): 4 contratos.
        for _ in range(4):
            _active_snapshot(
                organization_a, on=last_month_first, monthly=Decimal("100")
            )

        global _seq
        set_current_organization(organization_a)
        # 1 cancelado DENTRO do mês fechado → conta no churn (1/4 = 25%).
        _seq += 1
        Contract.objects.create(
            organization=organization_a,
            source_type="FAKE",
            external_id=f"kpi-closed-{_seq}",
            customer_external_id=f"kpi-closedc-{_seq}",
            plan_name="Plano X",
            monthly_amount=Decimal("100"),
            status="CANCELED",
            canceled_at=_aware(last_month_first + timedelta(days=10)),
        )
        # 1 cancelado no mês CORRENTE (parcial) → NÃO deve entrar no churn fechado.
        _seq += 1
        Contract.objects.create(
            organization=organization_a,
            source_type="FAKE",
            external_id=f"kpi-curr-{_seq}",
            customer_external_id=f"kpi-currc-{_seq}",
            plan_name="Plano X",
            monthly_amount=Decimal("100"),
            status="CANCELED",
            canceled_at=_aware(today),
        )

        kpis = compute_kpis(organization_a)
        assert kpis["churn_canceled"] == 1
        assert kpis["churn_pct"] == 25.0
        assert kpis["churn_month_label"] == last_month_first.strftime("%b/%y")
        # cancelado do mês corrente ainda aparece no MTD, separado do churn.
        assert kpis["canceled_this_month"] == 1


@pytest.mark.django_db
class TestComputeKpisSnapshotFallback:
    def test_mrr_uses_latest_snapshot_when_today_missing(
        self, organization_a: Organization
    ) -> None:
        # Sem snapshot para hoje (job diário ainda não rodou): o último <= hoje
        # deve alimentar o MRR em vez de zerar.
        yesterday = timezone.now().date() - timedelta(days=1)
        _active_snapshot(organization_a, on=yesterday, monthly=Decimal("250"))

        kpis = compute_kpis(organization_a)
        assert kpis["mrr_now"] == 250.0
        assert kpis["active_contracts"] == 1


@pytest.mark.django_db
class TestEquipmentFieldTrend:
    def test_only_active_with_valid_date_count(self, organization_a: Organization) -> None:
        today = timezone.now().date()
        recent = (today - timedelta(days=10)).isoformat()
        _make_equipment(organization_a, status="ACTIVE", data=recent)
        _make_equipment(organization_a, status="ACTIVE", data=recent)
        # devolvido não conta
        _make_equipment(organization_a, status="RETURNED", data=recent)
        # ativo sem data válida é ignorado graciosamente
        _make_equipment(organization_a, status="ACTIVE", data="lixo")
        _make_equipment(organization_a, status="ACTIVE", data=None)

        trend = compute_equipment_field_trend(organization_a, months=6)
        assert trend[-1]["count"] == 2

    def test_empty_is_graceful(self, organization_a: Organization) -> None:
        trend = compute_equipment_field_trend(organization_a, months=3)
        assert [r["count"] for r in trend] == [0, 0, 0]
