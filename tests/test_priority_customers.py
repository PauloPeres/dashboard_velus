"""Testes da priorização de clientes a focar em /customers/ (#28).

`compute_priority_customers` combina o score de churn materializado
(`ChurnRiskScore`, HIGH/MEDIUM) com o valor do cliente num índice de foco
(valor × risco), define a ação (COBRAR p/ sinal financeiro, senão RETER) e lista
candidatos a UPSELL (ACTIVE de maior MRR fora do radar de risco).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_priority_customers
from apps.analytics.infrastructure.models import (
    ChurnRiskScore,
    FactContractStatusDaily,
)
from apps.customers.infrastructure.models import Contract, Customer
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_customer(org: Organization, *, name: str, status: str = "ACTIVE") -> Customer:
    global _seq
    _seq += 1
    customer = Customer(
        organization=org,
        source_type="FAKE",
        external_id=f"pc-cust-{_seq}",
        document=f"{_seq:011d}",
        name=name,
        status=status,
    )
    customer.save()
    return customer


def _make_risk(
    org: Organization,
    customer: Customer,
    *,
    score: int,
    level: str,
    monthly: Decimal,
    signals: list[dict] | None = None,
    ml_probability: Decimal | None = None,
) -> ChurnRiskScore:
    set_current_organization(org)
    return ChurnRiskScore.objects.create(
        organization=org,
        customer=customer,
        score=score,
        level=level,
        signals=signals or [],
        monthly_amount=monthly,
        ml_probability=ml_probability,
        computed_at=timezone.now(),
    )


def _make_active_contract(
    org: Organization, customer: Customer, *, monthly: Decimal
) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    contract = Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"pc-ctr-{_seq}",
        customer=customer,
        customer_external_id=customer.external_id,
        plan_name="Plano X",
        monthly_amount=monthly,
        status="ACTIVE",
    )
    FactContractStatusDaily.objects.create(
        organization=org,
        contract=contract,
        date=timezone.now().date(),
        status="ACTIVE",
        monthly_amount=monthly,
        is_active=True,
    )


@pytest.mark.django_db
class TestComputePriorityCustomers:
    def test_empty_returns_zeros(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        result = compute_priority_customers(organization_a)
        assert result["focus"] == []
        assert result["upsell"] == []
        assert result["focus_count"] == 0
        assert result["revenue_in_focus"] == 0.0

    def test_focus_pool_excludes_low_level(
        self, organization_a: Organization
    ) -> None:
        high = _make_customer(organization_a, name="Alto Risco")
        low = _make_customer(organization_a, name="Baixo Risco")
        _make_risk(organization_a, high, score=80, level="HIGH", monthly=Decimal("200"))
        _make_risk(organization_a, low, score=10, level="LOW", monthly=Decimal("500"))
        set_current_organization(organization_a)

        result = compute_priority_customers(organization_a)
        ids = {f["customer_id"] for f in result["focus"]}
        assert high.id in ids
        assert low.id not in ids
        assert result["focus_count"] == 1

    def test_action_is_cobrar_for_payment_signal(
        self, organization_a: Organization
    ) -> None:
        c = _make_customer(organization_a, name="Devedor")
        _make_risk(
            organization_a, c, score=70, level="HIGH", monthly=Decimal("100"),
            signals=[{"code": "LATE_PAYMENTS", "label": "Atraso recorrente", "weight": 30}],
        )
        set_current_organization(organization_a)

        result = compute_priority_customers(organization_a)
        assert result["focus"][0]["action"] == "COBRAR"
        assert result["cobrar_count"] == 1
        assert "Atraso" in result["focus"][0]["reason"]

    def test_action_is_reter_for_service_signal(
        self, organization_a: Organization
    ) -> None:
        c = _make_customer(organization_a, name="Insatisfeito")
        _make_risk(
            organization_a, c, score=60, level="MEDIUM", monthly=Decimal("100"),
            signals=[{"code": "FREQUENT_TICKETS", "label": "Chamados frequentes", "weight": 20}],
        )
        set_current_organization(organization_a)

        result = compute_priority_customers(organization_a)
        assert result["focus"][0]["action"] == "RETER"
        assert result["reter_count"] == 1

    def test_focus_index_ranks_value_times_risk(
        self, organization_a: Organization
    ) -> None:
        # Mesmo risco máximo, valor maior → índice de foco maior.
        rich = _make_customer(organization_a, name="Caro")
        poor = _make_customer(organization_a, name="Barato")
        _make_risk(organization_a, rich, score=80, level="HIGH", monthly=Decimal("1000"))
        _make_risk(organization_a, poor, score=80, level="HIGH", monthly=Decimal("100"))
        set_current_organization(organization_a)

        result = compute_priority_customers(organization_a)
        assert result["focus"][0]["customer_id"] == rich.id
        assert result["focus"][0]["focus_index"] > result["focus"][1]["focus_index"]
        # Topo com risco e valor máximos → índice 100.
        assert result["focus"][0]["focus_index"] == 100.0

    def test_upsell_lists_healthy_high_value_outside_risk(
        self, organization_a: Organization
    ) -> None:
        healthy = _make_customer(organization_a, name="Saudável")
        at_risk = _make_customer(organization_a, name="Em Risco")
        _make_active_contract(organization_a, healthy, monthly=Decimal("300"))
        _make_active_contract(organization_a, at_risk, monthly=Decimal("400"))
        _make_risk(organization_a, at_risk, score=70, level="HIGH", monthly=Decimal("400"))
        set_current_organization(organization_a)

        result = compute_priority_customers(organization_a)
        upsell_ids = {u["customer_id"] for u in result["upsell"]}
        assert healthy.id in upsell_ids
        assert at_risk.id not in upsell_ids
        assert result["upsell_count"] == 1
