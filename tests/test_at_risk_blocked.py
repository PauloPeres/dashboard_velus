"""Testes da data de bloqueio dos contratos em risco (#29).

`compute_at_risk_contracts`/`compute_blocked_at_risk_summary` só sabiam há
quanto tempo um contrato estava bloqueado se havia um dia NÃO bloqueado no
histórico do `FactContractStatusDaily`. Contratos já bloqueados antes do início
dos snapshots caíam no sentinela `days_blocked=999` e `blocked_since="—"`.
O fallback agora usa a data real do IXC (`dt_ult_bloq_manual`/`dt_ult_bloq_auto`).
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _ixc_blocked_since,
    compute_at_risk_contracts,
    compute_blocked_at_risk_summary,
)
from apps.analytics.infrastructure.models import FactContractStatusDaily
from apps.customers.infrastructure.models import Contract
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_blocked_contract(
    org: Organization,
    *,
    raw_extras: dict | None = None,
    monthly_amount: str = "100.00",
) -> Contract:
    """Cria um contrato BLOCKED com snapshot de hoje, sem histórico anterior."""
    global _seq
    _seq += 1
    set_current_organization(org)
    contract = Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"ctr-{_seq}",
        customer_external_id=f"cust-{_seq}",
        plan_name="Plano X",
        monthly_amount=Decimal(monthly_amount),
        status="BLOCKED",
        raw_extras=raw_extras or {},
    )
    FactContractStatusDaily.objects.create(
        organization=org,
        contract=contract,
        date=timezone.now().date(),
        status="BLOCKED",
        monthly_amount=Decimal(monthly_amount),
        is_active=True,
    )
    return contract


class TestIxcBlockedSince:
    def test_picks_manual_block_date(self) -> None:
        assert _ixc_blocked_since({"dt_ult_bloq_manual": "2026-04-14"}) == date(2026, 4, 14)

    def test_picks_most_recent_of_manual_and_auto(self) -> None:
        extras = {"dt_ult_bloq_manual": "2026-04-14", "dt_ult_bloq_auto": "2026-05-01"}
        assert _ixc_blocked_since(extras) == date(2026, 5, 1)

    def test_ignores_empty_and_blank(self) -> None:
        assert _ixc_blocked_since({"dt_ult_bloq_manual": "", "dt_ult_bloq_auto": "  "}) is None

    def test_ignores_ixc_null_date_sentinel(self) -> None:
        # IXC envia '0000-00-00' como data nula — não pode virar blocked_since.
        extras = {"dt_ult_bloq_manual": "0000-00-00", "dt_ult_bloq_auto": "0000-00-00"}
        assert _ixc_blocked_since(extras) is None

    def test_falls_back_to_data_inicial_suspensao(self) -> None:
        # Bloqueio automático: dt_ult_bloq_* vazios, suspensão marca o início.
        extras = {"dt_ult_bloq_manual": "", "data_inicial_suspensao": "2026-05-15"}
        assert _ixc_blocked_since(extras) == date(2026, 5, 15)

    def test_tolerates_datetime_suffix(self) -> None:
        assert _ixc_blocked_since({"dt_ult_bloq_manual": "2026-04-14 17:18:39"}) == date(2026, 4, 14)

    def test_none_when_no_keys(self) -> None:
        assert _ixc_blocked_since({}) is None
        assert _ixc_blocked_since(None) is None


@pytest.mark.django_db
class TestAtRiskBlockedSinceFallback:
    def test_uses_ixc_date_when_no_history(self, organization_a: Organization) -> None:
        block_day = timezone.now().date() - timedelta(days=45)
        _make_blocked_contract(
            organization_a,
            raw_extras={"dt_ult_bloq_manual": block_day.isoformat()},
        )
        set_current_organization(organization_a)

        rows = compute_at_risk_contracts(organization_a, min_days=30)
        assert len(rows) == 1
        assert rows[0]["blocked_since"] == block_day.isoformat()
        assert rows[0]["days_blocked"] == 45

    def test_falls_back_to_sentinel_without_ixc_date(
        self, organization_a: Organization
    ) -> None:
        _make_blocked_contract(organization_a, raw_extras={})
        set_current_organization(organization_a)

        rows = compute_at_risk_contracts(organization_a, min_days=30)
        assert len(rows) == 1
        assert rows[0]["blocked_since"] == "—"
        assert rows[0]["days_blocked"] == 999

    def test_summary_counts_ixc_dated_contract(
        self, organization_a: Organization
    ) -> None:
        block_day = timezone.now().date() - timedelta(days=40)
        _make_blocked_contract(
            organization_a,
            raw_extras={"dt_ult_bloq_manual": block_day.isoformat()},
            monthly_amount="79.90",
        )
        set_current_organization(organization_a)

        summary = compute_blocked_at_risk_summary(organization_a, min_days=30)
        assert summary["count"] == 1
        assert summary["revenue_at_risk"] == pytest.approx(79.90)
        assert summary["pct_of_blocked"] == 100.0

    def test_recent_ixc_block_excluded_below_min_days(
        self, organization_a: Organization
    ) -> None:
        block_day = timezone.now().date() - timedelta(days=10)
        _make_blocked_contract(
            organization_a,
            raw_extras={"dt_ult_bloq_manual": block_day.isoformat()},
        )
        set_current_organization(organization_a)

        assert compute_at_risk_contracts(organization_a, min_days=30) == []
        assert compute_blocked_at_risk_summary(organization_a, min_days=30)["count"] == 0
