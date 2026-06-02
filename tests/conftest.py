"""Fixtures globais de teste.

Convenção:
- Org A e Org B disponíveis em toda suíte pra testar cross-tenant isolation
- FakeCustomerSource já registrado (vem do INSTALLED_APPS) — testes só fazem seed
- Contextvar de organização é limpo entre testes pra evitar leakage
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.financial.domain.dto import InvoiceDTO, PaymentDTO
from apps.helpdesk.domain.dto import TicketDTO
from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.equipment import FakeEquipmentSource
from apps.integrations.fake.invoices import FakeInvoiceSource, FakePaymentSource
from apps.integrations.fake.tickets import FakeTicketSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.inventory.domain.dto import EquipmentDTO
from apps.network.domain.dto import ConnectionDTO
from apps.shared.context import set_current_organization
from apps.tenancy.models import (
    Organization,
    OrganizationDataSource,
    OrganizationMembership,
    User,
)


# =============================================================================
# Setup/teardown global — isola contextvar e seed do Fake entre testes
# =============================================================================
@pytest.fixture(autouse=True)
def _clean_state_around_test() -> Iterator[None]:
    """Cleanup pré/pós teste — contextvar + seeds dos Fake adapters."""
    set_current_organization(None)
    FakeCustomerSource.reset_seed()
    FakeContractSource.reset_seed()
    FakeInvoiceSource.reset_seed()
    FakePaymentSource.reset_seed()
    FakeTicketSource.reset_seed()
    FakeConnectionSource.reset_seed()
    FakeEquipmentSource.reset_seed()
    try:
        yield
    finally:
        set_current_organization(None)
        FakeCustomerSource.reset_seed()
        FakeContractSource.reset_seed()
        FakeInvoiceSource.reset_seed()
        FakePaymentSource.reset_seed()
        FakeTicketSource.reset_seed()
        FakeConnectionSource.reset_seed()


# =============================================================================
# Organizações
# =============================================================================
@pytest.fixture
def organization_a(db) -> Organization:
    return Organization.objects.create(slug="acme", name="ACME ISP")


@pytest.fixture
def organization_b(db) -> Organization:
    return Organization.objects.create(slug="brava", name="Brava Net")


# =============================================================================
# Users
# =============================================================================
@pytest.fixture
def user_a(db, organization_a: Organization) -> User:
    user = User.objects.create_user(email="owner-a@acme.test")
    OrganizationMembership.objects.create(
        user=user,
        organization=organization_a,
        role=OrganizationMembership.Role.OWNER,
        is_active=True,
    )
    return user


@pytest.fixture
def user_b(db, organization_b: Organization) -> User:
    user = User.objects.create_user(email="owner-b@brava.test")
    OrganizationMembership.objects.create(
        user=user,
        organization=organization_b,
        role=OrganizationMembership.Role.OWNER,
        is_active=True,
    )
    return user


# =============================================================================
# OrganizationDataSource configurando Fake
# =============================================================================
@pytest.fixture
def datasource_fake_customers_a(db, organization_a: Organization) -> OrganizationDataSource:
    ds = OrganizationDataSource.objects.create(
        organization=organization_a,
        source_type=SourceType.FAKE.value,
        capability=Capability.CUSTOMERS.value,
        priority=100,
        is_active=True,
    )
    ds.set_credentials({})
    ds.save()
    return ds


@pytest.fixture
def datasource_fake_customers_b(db, organization_b: Organization) -> OrganizationDataSource:
    ds = OrganizationDataSource.objects.create(
        organization=organization_b,
        source_type=SourceType.FAKE.value,
        capability=Capability.CUSTOMERS.value,
        priority=100,
        is_active=True,
    )
    ds.set_credentials({})
    ds.save()
    return ds


# =============================================================================
# CustomerDTOs reutilizáveis
# =============================================================================
@pytest.fixture
def sample_customer_dtos() -> list[CustomerDTO]:
    return [
        CustomerDTO(
            external_id="ext-1",
            document="12345678901",
            name="Cliente Um",
            email="um@example.test",
            status="ACTIVE",
            created_at_source=datetime(2025, 1, 10, tzinfo=UTC),
        ),
        CustomerDTO(
            external_id="ext-2",
            document="98765432101",
            name="Cliente Dois",
            status="BLOCKED",
            created_at_source=datetime(2025, 2, 15, tzinfo=UTC),
        ),
    ]


@pytest.fixture
def sample_contract_dtos() -> list[ContractDTO]:
    from decimal import Decimal
    return [
        ContractDTO(
            external_id="ctr-1",
            customer_external_id="ext-1",
            plan_name="Fibra 500M",
            monthly_amount=Decimal("150.00"),
            status="ACTIVE",
            activated_at=datetime(2025, 1, 15, tzinfo=UTC),
        ),
        ContractDTO(
            external_id="ctr-2",
            customer_external_id="ext-2",
            plan_name="Fibra 200M",
            monthly_amount=Decimal("100.00"),
            status="BLOCKED",
            activated_at=datetime(2025, 2, 20, tzinfo=UTC),
        ),
    ]


@pytest.fixture
def sample_invoice_dtos() -> list[InvoiceDTO]:
    from datetime import date
    from decimal import Decimal
    return [
        InvoiceDTO(
            external_id="inv-1",
            contract_external_id="ctr-1",
            amount=Decimal("150.00"),
            due_date=date(2025, 4, 10),
            status="PAID",
            paid_at=datetime(2025, 4, 8, tzinfo=UTC),
            paid_amount=Decimal("150.00"),
        ),
        InvoiceDTO(
            external_id="inv-2",
            contract_external_id="ctr-1",
            amount=Decimal("150.00"),
            due_date=date(2025, 5, 10),
            status="PENDING",
        ),
    ]


@pytest.fixture
def sample_payment_dtos() -> list[PaymentDTO]:
    from decimal import Decimal
    return [
        PaymentDTO(
            external_id="pay-1",
            invoice_external_id="inv-1",
            contract_external_id="ctr-1",
            amount=Decimal("150.00"),
            paid_at=datetime(2025, 4, 8, tzinfo=UTC),
            method="PIX",
        ),
    ]


@pytest.fixture
def sample_ticket_dtos() -> list[TicketDTO]:
    return [
        TicketDTO(
            external_id="tk-1",
            customer_external_id="ext-1",
            subject_id="10",
            sector="Suporte",
            technician_id="3",
            status="OPEN",
            priority="HIGH",
            message="Sem conexão",
            protocol="2025001",
            opened_at=datetime(2025, 5, 10, 9, 0, tzinfo=UTC),
        ),
        TicketDTO(
            external_id="tk-2",
            customer_external_id="ext-2",
            subject_id="11",
            sector="Financeiro",
            technician_id="",
            status="CLOSED",
            priority="NORMAL",
            message="Dúvida de fatura",
            protocol="2025002",
            opened_at=datetime(2025, 5, 12, 14, 0, tzinfo=UTC),
            closed_at=datetime(2025, 5, 12, 16, 0, tzinfo=UTC),
        ),
    ]


@pytest.fixture
def sample_connection_dtos() -> list[ConnectionDTO]:
    return [
        ConnectionDTO(
            external_id="conn-1",
            customer_external_id="ext-1",
            contract_external_id="ctr-1",
            login="cliente1",
            status="ONLINE",
            ip="10.0.0.1",
            nas_ip="192.168.1.10",
            rx_bytes=1_073_741_824,
            tx_bytes=536_870_912,
            last_connection_at=datetime(2025, 5, 20, 8, 0, tzinfo=UTC),
        ),
        ConnectionDTO(
            external_id="conn-2",
            customer_external_id="ext-2",
            contract_external_id="ctr-2",
            login="cliente2",
            status="BLOCKED",
            ip="",
            nas_ip="192.168.1.10",
            rx_bytes=0,
            tx_bytes=0,
            last_connection_at=datetime(2025, 5, 18, 12, 0, tzinfo=UTC),
        ),
    ]


@pytest.fixture
def sample_equipment_dtos() -> list[EquipmentDTO]:
    from decimal import Decimal

    return [
        EquipmentDTO(
            external_id="eq-1",
            contract_external_id="ctr-1",
            product_name="ONT Huawei HG8245",
            status="ACTIVE",
            serial="SN-0001",
            mac="AA:BB:CC:00:00:01",
            value=Decimal("250.00"),
        ),
        EquipmentDTO(
            external_id="eq-2",
            contract_external_id="ctr-2",
            product_name="Roteador TP-Link",
            status="RETURNED",
            serial="SN-0002",
            mac="AA:BB:CC:00:00:02",
            value=Decimal("120.00"),
        ),
    ]


def _make_datasource_factory(capability: Capability) -> Any:
    """Helper pra criar fixture de OrganizationDataSource FAKE pra qualquer capability."""
    @pytest.fixture
    def _ds(db, organization_a: Organization) -> OrganizationDataSource:
        ds = OrganizationDataSource.objects.create(
            organization=organization_a,
            source_type=SourceType.FAKE.value,
            capability=capability.value,
            priority=100,
            is_active=True,
        )
        ds.set_credentials({})
        ds.save()
        return ds
    return _ds


datasource_fake_contracts_a = _make_datasource_factory(Capability.CONTRACTS)
datasource_fake_invoices_a = _make_datasource_factory(Capability.INVOICES)
datasource_fake_payments_a = _make_datasource_factory(Capability.PAYMENTS)
datasource_fake_tickets_a = _make_datasource_factory(Capability.TICKETS)
datasource_fake_connections_a = _make_datasource_factory(Capability.CONNECTIONS)
datasource_fake_equipment_a = _make_datasource_factory(Capability.EQUIPMENT)
