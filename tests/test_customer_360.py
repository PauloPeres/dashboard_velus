"""Testes do Customer 360 — busca de clientes + agregação cross-app + views.

Cobre `search_customers`, `compute_customer_360` (junção customers + financial +
helpdesk + network + inventory) e as views de lista/detalhe.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.analytics.application.aggregations import (
    compute_customer_360,
    search_customers,
)
from apps.customers.infrastructure.models import Customer
from apps.integrations.fake.bandwidth import FakeBandwidthUsageSource
from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.equipment import FakeEquipmentSource
from apps.integrations.fake.invoices import FakeInvoiceSource, FakePaymentSource
from apps.integrations.fake.tickets import FakeTicketSource
from apps.shared.context import set_current_organization
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource, User


def _sync(org: Organization, capability: str) -> None:
    sync_capability(organization_id=org.pk, capability=capability, mode="BOOTSTRAP")


@pytest.fixture
def seeded_360(
    organization_a: Organization,
    datasource_fake_customers_a: OrganizationDataSource,
    datasource_fake_contracts_a: OrganizationDataSource,
    datasource_fake_invoices_a: OrganizationDataSource,
    datasource_fake_payments_a: OrganizationDataSource,
    datasource_fake_tickets_a: OrganizationDataSource,
    datasource_fake_connections_a: OrganizationDataSource,
    datasource_fake_bandwidth_a: OrganizationDataSource,
    datasource_fake_equipment_a: OrganizationDataSource,
    sample_customer_dtos: list[Any],
    sample_contract_dtos: list[Any],
    sample_invoice_dtos: list[Any],
    sample_payment_dtos: list[Any],
    sample_ticket_dtos: list[Any],
    sample_connection_dtos: list[Any],
    sample_bandwidth_dtos: list[Any],
    sample_equipment_dtos: list[Any],
) -> Organization:
    """Sincroniza todas as capabilities pra org A, na ordem de resolução de FK."""
    FakeCustomerSource.set_seed(sample_customer_dtos)
    _sync(organization_a, "CUSTOMERS")
    FakeContractSource.set_seed(sample_contract_dtos)
    _sync(organization_a, "CONTRACTS")
    FakeInvoiceSource.set_seed(sample_invoice_dtos)
    _sync(organization_a, "INVOICES")
    FakePaymentSource.set_seed(sample_payment_dtos)
    _sync(organization_a, "PAYMENTS")
    FakeTicketSource.set_seed(sample_ticket_dtos)
    _sync(organization_a, "TICKETS")
    FakeConnectionSource.set_seed(sample_connection_dtos)
    _sync(organization_a, "CONNECTIONS")
    FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)
    _sync(organization_a, "BANDWIDTH")
    FakeEquipmentSource.set_seed(sample_equipment_dtos)
    _sync(organization_a, "EQUIPMENT")
    return organization_a


def _get_customer(org: Organization, external_id: str) -> Customer:
    set_current_organization(org)
    return Customer.objects.get(external_id=external_id)


# =============================================================================
# search_customers
# =============================================================================
@pytest.mark.django_db
class TestSearchCustomers:
    def _seed_customers(
        self,
        org: Organization,
        ds: OrganizationDataSource,
        dtos: list[Any],
    ) -> None:
        FakeCustomerSource.set_seed(dtos)
        _sync(org, "CUSTOMERS")

    def test_empty_query_returns_all(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[Any],
    ) -> None:
        self._seed_customers(organization_a, datasource_fake_customers_a, sample_customer_dtos)
        results = search_customers(organization_a)
        assert len(results) == 2

    def test_search_by_name(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[Any],
    ) -> None:
        self._seed_customers(organization_a, datasource_fake_customers_a, sample_customer_dtos)
        results = search_customers(organization_a, query="Cliente Um")
        assert len(results) == 1
        assert results[0]["external_id"] == "ext-1"

    def test_search_by_document(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[Any],
    ) -> None:
        self._seed_customers(organization_a, datasource_fake_customers_a, sample_customer_dtos)
        results = search_customers(organization_a, query="98765432101")
        assert len(results) == 1
        assert results[0]["external_id"] == "ext-2"

    def test_search_by_external_id(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[Any],
    ) -> None:
        self._seed_customers(organization_a, datasource_fake_customers_a, sample_customer_dtos)
        results = search_customers(organization_a, query="ext-1")
        assert len(results) == 1
        assert results[0]["name"] == "Cliente Um"

    def test_no_match_returns_empty(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[Any],
    ) -> None:
        self._seed_customers(organization_a, datasource_fake_customers_a, sample_customer_dtos)
        assert search_customers(organization_a, query="inexistente") == []

    def test_includes_contract_counts(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
        sample_customer_dtos: list[Any],
        sample_contract_dtos: list[Any],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)
        _sync(organization_a, "CUSTOMERS")
        FakeContractSource.set_seed(sample_contract_dtos)
        _sync(organization_a, "CONTRACTS")
        results = {r["external_id"]: r for r in search_customers(organization_a)}
        assert results["ext-1"]["contract_count"] == 1
        assert results["ext-1"]["active_contracts"] == 1
        assert results["ext-2"]["active_contracts"] == 0  # ctr-2 é BLOCKED


# =============================================================================
# compute_customer_360
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestComputeCustomer360:
    def test_header(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        assert data["customer"]["name"] == "Cliente Um"
        assert data["customer"]["document"] == "12345678901"
        assert data["customer"]["status"] == "ACTIVE"

    def test_contracts(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        assert data["contracts_count"] == 1
        assert data["contracts"][0]["plan_name"] == "Fibra 500M"
        assert data["mrr_active"] == 150.0

    def test_financial_overdue_and_paid(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        fin = data["financial"]
        # inv-2 PENDING vencida (due 2025-05-10) → inadimplente
        assert fin["overdue_amount"] == 150.0
        assert fin["delinquent"] is True
        assert fin["paid_total"] == 150.0
        assert len(fin["recent_invoices"]) == 2
        assert len(fin["recent_payments"]) == 1

    def test_support(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        sup = data["support"]
        assert sup["open_count"] == 1  # tk-1 OPEN
        assert sup["total_count"] == 1
        # tk-1 não foi fechado → sem SLA computável pra este cliente
        assert sup["avg_resolution_hours"] is None

    def test_support_sla_computed_for_closed(self, seeded_360: Organization) -> None:
        # ext-2 tem tk-2 CLOSED (aberto 14h, fechado 16h → 2h)
        customer = _get_customer(seeded_360, "ext-2")
        data = compute_customer_360(seeded_360, customer)
        assert data["support"]["open_count"] == 0
        assert data["support"]["avg_resolution_hours"] == 2.0

    def test_network(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        net = data["network"]
        assert len(net["connections"]) == 1
        assert net["connections"][0]["status"] == "ONLINE"
        # bw-1: 5 GB download + 1 GB upload
        assert net["download_bytes"] == 5_368_709_120
        assert net["total_gb"] == 6.0

    def test_equipment(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        assert len(data["equipment"]) == 1
        assert data["equipment"][0]["serial"] == "SN-0001"

    def test_timeline_sorted_desc(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        timeline = data["timeline"]
        assert len(timeline) >= 3
        ats = [ev["at"] for ev in timeline]
        assert ats == sorted(ats, reverse=True)
        types = {ev["type"] for ev in timeline}
        assert "customer_created" in types
        assert "contract_activated" in types

    def test_isolated_per_customer(self, seeded_360: Organization) -> None:
        # ext-2 não deve ver dados de ext-1
        customer = _get_customer(seeded_360, "ext-2")
        data = compute_customer_360(seeded_360, customer)
        assert data["contracts_count"] == 1
        assert data["contracts"][0]["plan_name"] == "Fibra 200M"
        assert data["network"]["connections"][0]["status"] == "BLOCKED"


# =============================================================================
# Link de WhatsApp (#37)
# =============================================================================
class TestWhatsappLink:
    def test_eleven_digits_gets_country_code(self) -> None:
        from apps.analytics.application.aggregations import _whatsapp_link

        assert _whatsapp_link("(11) 98765-4321") == "https://wa.me/5511987654321"

    def test_ten_digits_gets_country_code(self) -> None:
        from apps.analytics.application.aggregations import _whatsapp_link

        assert _whatsapp_link("1133334444") == "https://wa.me/551133334444"

    def test_already_with_country_code_kept(self) -> None:
        from apps.analytics.application.aggregations import _whatsapp_link

        assert _whatsapp_link("5511987654321") == "https://wa.me/5511987654321"

    def test_empty_or_invalid_returns_none(self) -> None:
        from apps.analytics.application.aggregations import _whatsapp_link

        assert _whatsapp_link("") is None
        assert _whatsapp_link(None) is None
        assert _whatsapp_link("123") is None


# =============================================================================
# Seção de risco de churn no 360 (#37)
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestCustomer360Churn:
    def test_no_risk_score_returns_none(self, seeded_360: Organization) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        data = compute_customer_360(seeded_360, customer)
        assert data["churn"] is None

    def test_risk_section_with_signals_and_recommendations(
        self, seeded_360: Organization
    ) -> None:
        from decimal import Decimal

        from django.utils import timezone

        from apps.analytics.infrastructure.models import ChurnRiskScore

        customer = _get_customer(seeded_360, "ext-1")
        set_current_organization(seeded_360)
        ChurnRiskScore.objects.create(
            organization=seeded_360,
            customer=customer,
            score=60,
            level=ChurnRiskScore.LEVEL_HIGH,
            signals=[
                {"code": "LATE_PAYMENTS", "label": "Atraso recorrente",
                 "detail": "3 faturas", "weight": 25},
                {"code": "OFFLINE", "label": "Offline com contrato ativo",
                 "detail": "", "weight": 15},
            ],
            monthly_amount=Decimal("150.00"),
            ml_probability=Decimal("0.7"),
            computed_at=timezone.now(),
        )

        data = compute_customer_360(seeded_360, customer)
        churn = data["churn"]
        assert churn is not None
        assert churn["level"] == "HIGH"
        assert churn["score"] == 60
        assert churn["ml_probability_pct"] == 70
        assert len(churn["signals"]) == 2
        codes = {r["code"] for r in churn["recommendations"]}
        assert codes == {"LATE_PAYMENTS", "OFFLINE"}
        assert all(r["text"] for r in churn["recommendations"])


# =============================================================================
# Views — lista + detalhe
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestCustomer360Views:
    def test_list_requires_login(self, client: Any) -> None:
        resp = client.get("/customers/")
        assert resp.status_code == 302  # redirect pro login

    def test_list_renders(
        self,
        client: Any,
        user_a: User,
        seeded_360: Organization,
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/customers/")
        assert resp.status_code == 200
        assert b"Cliente Um" in resp.content

    def test_list_search_filter(
        self,
        client: Any,
        user_a: User,
        seeded_360: Organization,
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/customers/", {"q": "Cliente Dois"})
        assert resp.status_code == 200
        assert b"Cliente Dois" in resp.content
        assert b"Cliente Um" not in resp.content

    def test_detail_renders(
        self,
        client: Any,
        user_a: User,
        seeded_360: Organization,
    ) -> None:
        customer = _get_customer(seeded_360, "ext-1")
        client.force_login(user_a)
        resp = client.get(f"/customers/{customer.pk}/")
        assert resp.status_code == 200
        assert b"Cliente Um" in resp.content
        assert b"Fibra 500M" in resp.content

    def test_detail_404_for_unknown(
        self,
        client: Any,
        user_a: User,
        organization_a: Organization,
    ) -> None:
        client.force_login(user_a)
        resp = client.get("/customers/999999/")
        assert resp.status_code == 404

    def test_detail_404_for_other_org_customer(
        self,
        client: Any,
        user_b: User,
        seeded_360: Organization,
    ) -> None:
        # customer pertence à org A; user_b é de org B → não pode ver
        customer = _get_customer(seeded_360, "ext-1")
        client.force_login(user_b)
        resp = client.get(f"/customers/{customer.pk}/")
        assert resp.status_code == 404
