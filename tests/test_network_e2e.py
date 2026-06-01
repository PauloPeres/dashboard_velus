"""Testes e2e de network — ConnectionRepository + sync ponta a ponta com FakeConnectionSource.

Roda toda a pipeline: registry → adapter → DTO → repository → DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.customers.domain.dto import CustomerDTO
from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.shared.enums import SourceType
from apps.network.domain.dto import ConnectionDTO
from apps.network.infrastructure.models import Connection
from apps.network.infrastructure.repositories import ConnectionRepository
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint, SyncJob, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


# =============================================================================
# ConnectionRepository — upsert idempotente, FK, normalização
# =============================================================================
@pytest.mark.django_db
class TestConnectionRepository:
    def _dto(self, **overrides: object) -> ConnectionDTO:
        base = {
            "external_id": "conn-1",
            "customer_external_id": "ext-1",
            "contract_external_id": "ctr-1",
            "login": "cliente1",
            "status": "ONLINE",
            "ip": "10.0.0.1",
            "nas_ip": "192.168.1.10",
            "rx_bytes": 1024,
            "tx_bytes": 512,
            "last_connection_at": datetime(2025, 5, 20, 8, 0, tzinfo=UTC),
        }
        base.update(overrides)
        return ConnectionDTO(**base)  # type: ignore[arg-type]

    def test_creates_connection(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = ConnectionRepository(organization_a)
        conn, created = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert created is True
        assert conn.external_id == "conn-1"
        assert conn.status == "ONLINE"
        assert conn.login == "cliente1"

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = ConnectionRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(status="OFFLINE"), source_type=SourceType.IXC
        )
        assert created is False

        set_current_organization(organization_a)
        assert Connection.objects.count() == 1
        assert Connection.objects.get(external_id="conn-1").status == "OFFLINE"

    def test_resolves_customer_fk(
        self, organization_a: Organization, datasource_fake_customers_a: OrganizationDataSource
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
        repo = ConnectionRepository(organization_a)
        conn, _ = repo.upsert_from_dto(self._dto(), source_type=SourceType.FAKE)
        assert conn.customer is not None
        assert conn.customer.name == "Cliente Um"

    def test_persists_with_null_fk_when_customer_absent(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        repo = ConnectionRepository(organization_a)
        conn, _ = repo.upsert_from_dto(
            self._dto(customer_external_id="ghost-999"), source_type=SourceType.IXC
        )
        assert conn.customer is None
        assert conn.customer_external_id == "ghost-999"

    def test_unknown_status_normalized(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = ConnectionRepository(organization_a)
        conn, _ = repo.upsert_from_dto(
            self._dto(status="WHATEVER"), source_type=SourceType.IXC
        )
        assert conn.status == Connection.Status.UNKNOWN.value


# =============================================================================
# Sync e2e — pipeline completa com FakeConnectionSource
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestConnectionSyncE2E:
    def test_bootstrap_persists_all_connections(
        self,
        organization_a: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
        sample_connection_dtos: list[ConnectionDTO],
    ) -> None:
        FakeConnectionSource.set_seed(sample_connection_dtos)

        result = sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="BOOTSTRAP"
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        conns = list(Connection.objects.all().order_by("external_id"))
        assert len(conns) == 2
        assert conns[0].status == "ONLINE"
        assert conns[1].status == "BLOCKED"

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
        sample_connection_dtos: list[ConnectionDTO],
    ) -> None:
        FakeConnectionSource.set_seed(sample_connection_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="BOOTSTRAP"
        )
        sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        assert Connection.objects.count() == 2

    def test_sync_creates_job_and_checkpoint(
        self,
        organization_a: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
        sample_connection_dtos: list[ConnectionDTO],
    ) -> None:
        FakeConnectionSource.set_seed(sample_connection_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="BOOTSTRAP"
        )

        job = SyncJob.objects.filter(organization=organization_a).first()
        assert job is not None
        assert job.status == SyncStatus.COMPLETED
        assert job.records_processed == 2

        checkpoint = SyncCheckpoint.objects.get(
            organization=organization_a, source_type="FAKE", capability="CONNECTIONS"
        )
        assert checkpoint.last_processed_at is not None

    def test_resolves_customer_fk_when_customers_synced_first(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_connections_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
        sample_connection_dtos: list[ConnectionDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CUSTOMERS", mode="BOOTSTRAP"
        )
        FakeConnectionSource.set_seed(sample_connection_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        conn1 = Connection.objects.get(external_id="conn-1")
        assert conn1.customer is not None
        assert conn1.customer.name == "Cliente Um"

    def test_incremental_skips_old_connections(
        self,
        organization_a: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
        sample_connection_dtos: list[ConnectionDTO],
    ) -> None:
        FakeConnectionSource.set_seed(sample_connection_dtos)
        SyncCheckpoint.objects.create(
            organization=organization_a,
            source_type="FAKE",
            capability="CONNECTIONS",
            last_processed_at=datetime(2025, 5, 19, tzinfo=UTC),
        )

        result = sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="INCREMENTAL"
        )

        # conn-2 (18/05) é anterior ao checkpoint → filtrado; conn-1 (20/05) entra
        assert result["records_processed"] == 1
        set_current_organization(organization_a)
        assert Connection.objects.count() == 1
        assert Connection.objects.first().external_id == "conn-1"

    def test_org_b_does_not_see_org_a_connections(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_connections_a: OrganizationDataSource,
        sample_connection_dtos: list[ConnectionDTO],
    ) -> None:
        FakeConnectionSource.set_seed(sample_connection_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CONNECTIONS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_b)
        assert Connection.objects.count() == 0

        set_current_organization(organization_a)
        assert Connection.objects.count() == 2
