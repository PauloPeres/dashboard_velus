"""Testes e2e do helpdesk — TicketRepository + sync ponta a ponta com FakeTicketSource.

Roda toda a pipeline: registry → adapter → DTO → repository → DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.customers.domain.dto import CustomerDTO
from apps.helpdesk.domain.dto import TicketDTO
from apps.helpdesk.infrastructure.models import Ticket
from apps.helpdesk.infrastructure.repositories import TicketRepository
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.tickets import FakeTicketSource
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint, SyncJob, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


# =============================================================================
# TicketRepository — upsert idempotente, FK, normalização
# =============================================================================
@pytest.mark.django_db
class TestTicketRepository:
    def _dto(self, **overrides: object) -> TicketDTO:
        base = {
            "external_id": "tk-1",
            "customer_external_id": "ext-1",
            "subject_id": "10",
            "sector": "Suporte",
            "technician_id": "3",
            "status": "OPEN",
            "priority": "HIGH",
            "message": "Sem conexão",
            "protocol": "2025001",
            "opened_at": datetime(2025, 5, 10, 9, 0, tzinfo=UTC),
        }
        base.update(overrides)
        return TicketDTO(**base)  # type: ignore[arg-type]

    def test_creates_ticket(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = TicketRepository(organization_a)
        ticket, created = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert created is True
        assert ticket.external_id == "tk-1"
        assert ticket.status == "OPEN"
        assert ticket.priority == "HIGH"

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = TicketRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(status="CLOSED"), source_type=SourceType.IXC
        )
        assert created is False

        set_current_organization(organization_a)
        assert Ticket.objects.count() == 1
        assert Ticket.objects.get(external_id="tk-1").status == "CLOSED"

    def test_resolves_customer_fk(
        self, organization_a: Organization, datasource_fake_customers_a: OrganizationDataSource
    ) -> None:
        # Cria o customer correspondente via sync
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

        # Customer foi sincronizado pela fonte FAKE — FK resolve por (org, source_type, ext_id)
        set_current_organization(organization_a)
        repo = TicketRepository(organization_a)
        ticket, _ = repo.upsert_from_dto(self._dto(), source_type=SourceType.FAKE)
        assert ticket.customer is not None
        assert ticket.customer.name == "Cliente Um"

    def test_persists_with_null_fk_when_customer_absent(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        repo = TicketRepository(organization_a)
        ticket, _ = repo.upsert_from_dto(
            self._dto(customer_external_id="ghost-999"), source_type=SourceType.IXC
        )
        assert ticket.customer is None
        assert ticket.customer_external_id == "ghost-999"

    def test_unknown_status_normalized(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = TicketRepository(organization_a)
        ticket, _ = repo.upsert_from_dto(
            self._dto(status="WHATEVER", priority="???"), source_type=SourceType.IXC
        )
        assert ticket.status == Ticket.Status.UNKNOWN.value
        assert ticket.priority == Ticket.Priority.UNKNOWN.value


# =============================================================================
# Sync e2e — pipeline completa com FakeTicketSource
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestTicketSyncE2E:
    def test_bootstrap_persists_all_tickets(
        self,
        organization_a: Organization,
        datasource_fake_tickets_a: OrganizationDataSource,
        sample_ticket_dtos: list[TicketDTO],
    ) -> None:
        FakeTicketSource.set_seed(sample_ticket_dtos)

        result = sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="BOOTSTRAP"
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        tickets = list(Ticket.objects.all().order_by("external_id"))
        assert len(tickets) == 2
        assert tickets[0].protocol == "2025001"
        assert tickets[1].status == "CLOSED"

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_tickets_a: OrganizationDataSource,
        sample_ticket_dtos: list[TicketDTO],
    ) -> None:
        FakeTicketSource.set_seed(sample_ticket_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="BOOTSTRAP"
        )
        sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        assert Ticket.objects.count() == 2

    def test_sync_creates_job_and_checkpoint(
        self,
        organization_a: Organization,
        datasource_fake_tickets_a: OrganizationDataSource,
        sample_ticket_dtos: list[TicketDTO],
    ) -> None:
        FakeTicketSource.set_seed(sample_ticket_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="BOOTSTRAP"
        )

        job = SyncJob.objects.filter(organization=organization_a).first()
        assert job is not None
        assert job.status == SyncStatus.COMPLETED
        assert job.records_processed == 2

        checkpoint = SyncCheckpoint.objects.get(
            organization=organization_a, source_type="FAKE", capability="TICKETS"
        )
        assert checkpoint.last_processed_at is not None

    def test_resolves_customer_fk_when_customers_synced_first(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_tickets_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
        sample_ticket_dtos: list[TicketDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CUSTOMERS", mode="BOOTSTRAP"
        )
        FakeTicketSource.set_seed(sample_ticket_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        tk1 = Ticket.objects.get(external_id="tk-1")
        assert tk1.customer is not None
        assert tk1.customer.name == "Cliente Um"

    def test_incremental_skips_old_tickets(
        self,
        organization_a: Organization,
        datasource_fake_tickets_a: OrganizationDataSource,
        sample_ticket_dtos: list[TicketDTO],
    ) -> None:
        FakeTicketSource.set_seed(sample_ticket_dtos)
        SyncCheckpoint.objects.create(
            organization=organization_a,
            source_type="FAKE",
            capability="TICKETS",
            last_processed_at=datetime(2025, 5, 11, tzinfo=UTC),
        )

        result = sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="INCREMENTAL"
        )

        # tk-1 (10/05) é anterior ao checkpoint → filtrado; tk-2 (12/05) entra
        assert result["records_processed"] == 1
        set_current_organization(organization_a)
        assert Ticket.objects.count() == 1
        assert Ticket.objects.first().external_id == "tk-2"

    def test_org_b_does_not_see_org_a_tickets(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_tickets_a: OrganizationDataSource,
        sample_ticket_dtos: list[TicketDTO],
    ) -> None:
        FakeTicketSource.set_seed(sample_ticket_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="TICKETS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_b)
        assert Ticket.objects.count() == 0

        set_current_organization(organization_a)
        assert Ticket.objects.count() == 2
