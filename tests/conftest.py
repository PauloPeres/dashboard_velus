"""Fixtures globais de teste.

Convenção:
- Org A e Org B disponíveis em toda suíte pra testar cross-tenant isolation
- FakeCustomerSource já registrado (vem do INSTALLED_APPS) — testes só fazem seed
- Contextvar de organização é limpo entre testes pra evitar leakage
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from apps.customers.domain.dto import CustomerDTO
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.shared.enums import Capability, SourceType
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
    """Cleanup pré/pós teste — contextvar + seed do FakeCustomerSource.

    Hard-set em vez de token-dance pra garantir isolamento entre testes mesmo
    se um teste anterior não limpou seu próprio estado.
    """
    set_current_organization(None)
    FakeCustomerSource.reset_seed()
    try:
        yield
    finally:
        set_current_organization(None)
        FakeCustomerSource.reset_seed()


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
