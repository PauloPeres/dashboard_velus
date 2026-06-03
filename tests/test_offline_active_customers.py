"""Testes de compute_offline_active_customers (#74).

Clientes com contrato ATIVO mas sem conexão online = receita em risco. Cobre:
exclusão de contratos não-ativos, exclusão de clientes com alguma conexão
online, soma do MRR líquido em risco e ordenação por tempo offline.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_offline_active_customers
from apps.customers.infrastructure.models import Contract, Customer
from apps.network.infrastructure.models import Connection
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _customer(org: Organization) -> Customer:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Customer.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cust-{_seq}",
        document=f"{_seq:011d}",
        name=f"Cliente {_seq}",
        status="ACTIVE",
    )


def _contract(
    org: Organization,
    customer: Customer | None,
    *,
    status: str,
    monthly: Decimal = Decimal("100"),
    addons: Decimal = Decimal("0"),
    discounts: Decimal = Decimal("0"),
) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"ctr-{_seq}",
        customer=customer,
        customer_external_id=customer.external_id if customer else "0",
        plan_name="Plano X",
        monthly_amount=monthly,
        monthly_amount_addons=addons,
        monthly_amount_discounts=discounts,
        status=status,
    )


def _connection(
    org: Organization,
    customer: Customer,
    *,
    status: str,
    last_connection_at=None,
) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    Connection.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"conn-{_seq}",
        customer=customer,
        customer_external_id=customer.external_id,
        login=f"login-{_seq}",
        status=status,
        last_connection_at=last_connection_at,
    )


@pytest.mark.django_db
class TestOfflineActiveCustomers:
    def test_offline_active_is_listed_with_net_mrr(
        self, organization_a: Organization
    ) -> None:
        cust = _customer(organization_a)
        # MRR líquido = 100 + 20 - 5 = 115
        _contract(
            organization_a,
            cust,
            status="ACTIVE",
            monthly=Decimal("100"),
            addons=Decimal("20"),
            discounts=Decimal("5"),
        )
        _connection(organization_a, cust, status="OFFLINE")

        out = compute_offline_active_customers(organization_a)
        assert out["count"] == 1
        assert out["mrr_at_risk"] == 115.0
        assert out["rows"][0]["customer_id"] == cust.id
        assert out["rows"][0]["mrr"] == 115.0

    def test_online_connection_excludes_customer(
        self, organization_a: Organization
    ) -> None:
        cust = _customer(organization_a)
        _contract(organization_a, cust, status="ACTIVE")
        # tem conexão offline E online → está conectado por outro login, não conta
        _connection(organization_a, cust, status="OFFLINE")
        _connection(organization_a, cust, status="ONLINE")

        out = compute_offline_active_customers(organization_a)
        assert out["count"] == 0
        assert out["mrr_at_risk"] == 0.0

    def test_non_active_contract_excluded(
        self, organization_a: Organization
    ) -> None:
        cust = _customer(organization_a)
        _contract(organization_a, cust, status="CANCELED")
        _connection(organization_a, cust, status="OFFLINE")

        assert compute_offline_active_customers(organization_a)["count"] == 0

    def test_active_online_only_excluded(
        self, organization_a: Organization
    ) -> None:
        cust = _customer(organization_a)
        _contract(organization_a, cust, status="ACTIVE")
        _connection(organization_a, cust, status="ONLINE")

        assert compute_offline_active_customers(organization_a)["count"] == 0

    def test_ordered_by_days_offline_desc_nulls_last(
        self, organization_a: Organization
    ) -> None:
        now = timezone.now()
        old = _customer(organization_a)
        _contract(organization_a, old, status="ACTIVE")
        _connection(
            organization_a, old, status="OFFLINE",
            last_connection_at=now - timedelta(days=40),
        )
        recent = _customer(organization_a)
        _contract(organization_a, recent, status="ACTIVE")
        _connection(
            organization_a, recent, status="OFFLINE",
            last_connection_at=now - timedelta(days=2),
        )
        never = _customer(organization_a)
        _contract(organization_a, never, status="ACTIVE")
        _connection(organization_a, never, status="OFFLINE", last_connection_at=None)

        rows = compute_offline_active_customers(organization_a)["rows"]
        assert [r["customer_id"] for r in rows] == [old.id, recent.id, never.id]
        assert rows[-1]["days_offline"] is None

    def test_empty_org_is_graceful(self, organization_a: Organization) -> None:
        out = compute_offline_active_customers(organization_a)
        assert out == {"count": 0, "mrr_at_risk": 0.0, "rows": []}
