"""Testes end-to-end de sync com FakeCustomerSource.

Roda toda a pipeline: registry → adapter → DTO → repository → DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.customers.domain.dto import CustomerDTO
from apps.customers.infrastructure.models import Customer
from apps.integrations.fake.customers import FakeCustomerSource
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint, SyncJob, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


@pytest.mark.django_db
@pytest.mark.e2e
class TestSyncE2EWithFakeSource:
    def test_bootstrap_persists_all_customers(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)

        result = sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="BOOTSTRAP",
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        customers = list(Customer.objects.all().order_by("external_id"))
        assert len(customers) == 2
        assert customers[0].name == "Cliente Um"
        assert customers[1].name == "Cliente Dois"

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)

        sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="BOOTSTRAP",
        )
        sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="BOOTSTRAP",
        )

        set_current_organization(organization_a)
        assert Customer.objects.count() == 2

    def test_sync_creates_job_and_updates_checkpoint(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)

        sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="BOOTSTRAP",
        )

        jobs = SyncJob.objects.filter(organization=organization_a)
        assert jobs.count() == 1
        job = jobs.first()
        assert job is not None
        assert job.status == SyncStatus.COMPLETED
        assert job.records_processed == 2

        checkpoint = SyncCheckpoint.objects.get(
            organization=organization_a,
            source_type="FAKE",
            capability="CUSTOMERS",
        )
        assert checkpoint.last_processed_at is not None

    def test_incremental_skips_old_records(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
    ) -> None:
        old = CustomerDTO(
            external_id="old-1",
            document="11111111111",
            name="Antigo",
            status="ACTIVE",
            created_at_source=datetime(2024, 1, 1, tzinfo=UTC),
        )
        new = CustomerDTO(
            external_id="new-1",
            document="22222222222",
            name="Novo",
            status="ACTIVE",
            created_at_source=datetime(2025, 6, 1, tzinfo=UTC),
        )
        FakeCustomerSource.set_seed([old, new])

        # Pre-popula checkpoint apontando pra meio de 2025
        SyncCheckpoint.objects.create(
            organization=organization_a,
            source_type="FAKE",
            capability="CUSTOMERS",
            last_processed_at=datetime(2025, 3, 1, tzinfo=UTC),
        )

        result = sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="INCREMENTAL",
        )

        # FakeCustomerSource respeita since e filtra "old-1"
        assert result["records_processed"] == 1

        set_current_organization(organization_a)
        assert Customer.objects.count() == 1
        assert Customer.objects.first().external_id == "new-1"

    def test_org_b_does_not_see_org_a_sync(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_customers_b: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
    ) -> None:
        """Sync de A não polui DB de B — isolamento por org."""
        FakeCustomerSource.set_seed(sample_customer_dtos)

        sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="BOOTSTRAP",
        )

        # B nunca foi sincronizada — não tem customers
        set_current_organization(organization_b)
        assert Customer.objects.count() == 0

        set_current_organization(organization_a)
        assert Customer.objects.count() == 2
