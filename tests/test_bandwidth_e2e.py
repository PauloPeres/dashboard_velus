"""Testes e2e de network — BandwidthUsageRepository + sync ponta a ponta com Fake.

Roda toda a pipeline: registry → adapter → DTO → repository → DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from apps.customers.domain.dto import CustomerDTO
from apps.integrations.fake.bandwidth import FakeBandwidthUsageSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.shared.enums import SourceType
from apps.network.domain.dto import BandwidthUsageDTO
from apps.network.infrastructure.models import BandwidthUsage
from apps.network.infrastructure.repositories import BandwidthUsageRepository
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint, SyncJob, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


# =============================================================================
# BandwidthUsageRepository — upsert idempotente, FK
# =============================================================================
@pytest.mark.django_db
class TestBandwidthUsageRepository:
    def _dto(self, **overrides: object) -> BandwidthUsageDTO:
        base = {
            "external_id": "bw-1",
            "customer_external_id": "ext-1",
            "download_bytes": 5_368_709_120,
            "upload_bytes": 1_073_741_824,
            "session_time": 86_400,
            "reference_date": date(2025, 5, 20),
        }
        base.update(overrides)
        return BandwidthUsageDTO(**base)  # type: ignore[arg-type]

    def test_creates_usage(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = BandwidthUsageRepository(organization_a)
        usage, created = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert created is True
        assert usage.external_id == "bw-1"
        assert usage.download_bytes == 5_368_709_120
        assert usage.upload_bytes == 1_073_741_824

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = BandwidthUsageRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(download_bytes=999), source_type=SourceType.IXC
        )
        assert created is False

        set_current_organization(organization_a)
        assert BandwidthUsage.objects.count() == 1
        assert BandwidthUsage.objects.get(external_id="bw-1").download_bytes == 999

    def test_resolves_customer_fk(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
    ) -> None:
        FakeCustomerSource.set_seed([
            CustomerDTO(
                external_id="ext-1",
                document="12345678901",
                name="Cliente Um",
                status="ACTIVE",
                created_at_source=datetime(2025, 1, 1, tzinfo=UTC),
            )
        ])
        sync_capability(
            organization_id=organization_a.pk, capability="CUSTOMERS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        repo = BandwidthUsageRepository(organization_a)
        usage, _ = repo.upsert_from_dto(self._dto(), source_type=SourceType.FAKE)
        assert usage.customer is not None
        assert usage.customer.name == "Cliente Um"

    def test_persists_with_null_fk_when_customer_absent(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        repo = BandwidthUsageRepository(organization_a)
        usage, _ = repo.upsert_from_dto(
            self._dto(customer_external_id="ghost-999"), source_type=SourceType.IXC
        )
        assert usage.customer is None
        assert usage.customer_external_id == "ghost-999"


# =============================================================================
# Sync e2e — pipeline completa com FakeBandwidthUsageSource
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestBandwidthSyncE2E:
    def test_bootstrap_persists_all_usage(
        self,
        organization_a: Organization,
        datasource_fake_bandwidth_a: OrganizationDataSource,
        sample_bandwidth_dtos: list[BandwidthUsageDTO],
    ) -> None:
        FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)

        result = sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="BOOTSTRAP"
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        items = list(BandwidthUsage.objects.all().order_by("external_id"))
        assert len(items) == 2
        assert items[0].download_bytes == 5_368_709_120

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_bandwidth_a: OrganizationDataSource,
        sample_bandwidth_dtos: list[BandwidthUsageDTO],
    ) -> None:
        FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="BOOTSTRAP"
        )
        sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        assert BandwidthUsage.objects.count() == 2

    def test_sync_creates_job_and_checkpoint(
        self,
        organization_a: Organization,
        datasource_fake_bandwidth_a: OrganizationDataSource,
        sample_bandwidth_dtos: list[BandwidthUsageDTO],
    ) -> None:
        FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="BOOTSTRAP"
        )

        job = SyncJob.objects.filter(organization=organization_a).first()
        assert job is not None
        assert job.status == SyncStatus.COMPLETED
        assert job.records_processed == 2

        checkpoint = SyncCheckpoint.objects.get(
            organization=organization_a, source_type="FAKE", capability="BANDWIDTH"
        )
        assert checkpoint.last_processed_at is not None

    def test_resolves_customer_fk_when_customers_synced_first(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_bandwidth_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
        sample_bandwidth_dtos: list[BandwidthUsageDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CUSTOMERS", mode="BOOTSTRAP"
        )
        FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        bw1 = BandwidthUsage.objects.get(external_id="bw-1")
        assert bw1.customer is not None
        assert bw1.customer.name == "Cliente Um"

    def test_incremental_skips_old_usage(
        self,
        organization_a: Organization,
        datasource_fake_bandwidth_a: OrganizationDataSource,
        sample_bandwidth_dtos: list[BandwidthUsageDTO],
    ) -> None:
        FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)
        SyncCheckpoint.objects.create(
            organization=organization_a,
            source_type="FAKE",
            capability="BANDWIDTH",
            last_processed_at=datetime(2025, 5, 19, tzinfo=UTC),
        )

        result = sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="INCREMENTAL"
        )

        # bw-2 (18/05) é anterior ao checkpoint → filtrado; bw-1 (20/05) entra
        assert result["records_processed"] == 1
        set_current_organization(organization_a)
        assert BandwidthUsage.objects.count() == 1
        assert BandwidthUsage.objects.first().external_id == "bw-1"

    def test_org_b_does_not_see_org_a_usage(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_bandwidth_a: OrganizationDataSource,
        sample_bandwidth_dtos: list[BandwidthUsageDTO],
    ) -> None:
        FakeBandwidthUsageSource.set_seed(sample_bandwidth_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="BANDWIDTH", mode="BOOTSTRAP"
        )

        set_current_organization(organization_b)
        assert BandwidthUsage.objects.count() == 0

        set_current_organization(organization_a)
        assert BandwidthUsage.objects.count() == 2
