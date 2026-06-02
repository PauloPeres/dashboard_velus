"""Testes e2e de sales/CRM — Lead/Opportunity repositories + sync ponta a ponta com Fakes.

Roda toda a pipeline: registry → adapter → DTO → repository → DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.integrations.fake.leads import FakeLeadSource
from apps.integrations.fake.opportunities import FakeOpportunitySource
from apps.integrations.shared.enums import SourceType
from apps.sales.domain.dto import LeadDTO, OpportunityDTO
from apps.sales.infrastructure.models import Lead, Opportunity
from apps.sales.infrastructure.repositories import (
    LeadRepository,
    OpportunityRepository,
)
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint, SyncJob, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


# =============================================================================
# LeadRepository — upsert idempotente, normalização
# =============================================================================
@pytest.mark.django_db
class TestLeadRepository:
    def _dto(self, **overrides: object) -> LeadDTO:
        base = {
            "external_id": "lead-1",
            "name": "Prospect Um",
            "status": "NEW",
            "phone": "11999990001",
            "email": "p1@example.test",
            "origin": "Indicação",
            "salesperson_id": "7",
            "created_at_source": datetime(2025, 4, 5, tzinfo=UTC),
        }
        base.update(overrides)
        return LeadDTO(**base)  # type: ignore[arg-type]

    def test_creates_lead(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = LeadRepository(organization_a)
        lead, created = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert created is True
        assert lead.external_id == "lead-1"
        assert lead.status == "NEW"
        assert lead.origin == "Indicação"

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = LeadRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(status="CONVERTED"), source_type=SourceType.IXC
        )
        assert created is False

        set_current_organization(organization_a)
        assert Lead.objects.count() == 1
        assert Lead.objects.get(external_id="lead-1").status == "CONVERTED"

    def test_unknown_status_normalized(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = LeadRepository(organization_a)
        lead, _ = repo.upsert_from_dto(
            self._dto(status="WHATEVER"), source_type=SourceType.IXC
        )
        assert lead.status == Lead.Status.UNKNOWN.value


# =============================================================================
# OpportunityRepository — upsert idempotente, FK, normalização
# =============================================================================
@pytest.mark.django_db
class TestOpportunityRepository:
    def _dto(self, **overrides: object) -> OpportunityDTO:
        base = {
            "external_id": "opp-1",
            "lead_external_id": "lead-1",
            "status": "OPEN",
            "value": Decimal("1200.00"),
            "created_at_source": datetime(2025, 4, 6, tzinfo=UTC),
        }
        base.update(overrides)
        return OpportunityDTO(**base)  # type: ignore[arg-type]

    def test_creates_opportunity(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = OpportunityRepository(organization_a)
        opp, created = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert created is True
        assert opp.external_id == "opp-1"
        assert opp.status == "OPEN"
        assert opp.value == Decimal("1200.00")

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = OpportunityRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(status="WON"), source_type=SourceType.IXC
        )
        assert created is False

        set_current_organization(organization_a)
        assert Opportunity.objects.count() == 1
        assert Opportunity.objects.get(external_id="opp-1").status == "WON"

    def test_resolves_lead_fk(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        lead_repo = LeadRepository(organization_a)
        lead_repo.upsert_from_dto(
            LeadDTO(external_id="lead-1", name="Prospect Um", status="NEW"),
            source_type=SourceType.IXC,
        )

        set_current_organization(organization_a)
        repo = OpportunityRepository(organization_a)
        opp, _ = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert opp.lead is not None
        assert opp.lead.external_id == "lead-1"

    def test_persists_with_null_fk_when_lead_absent(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        repo = OpportunityRepository(organization_a)
        opp, _ = repo.upsert_from_dto(
            self._dto(lead_external_id="ghost-999"), source_type=SourceType.IXC
        )
        assert opp.lead is None
        assert opp.lead_external_id == "ghost-999"

    def test_unknown_status_normalized(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = OpportunityRepository(organization_a)
        opp, _ = repo.upsert_from_dto(
            self._dto(status="WHATEVER"), source_type=SourceType.IXC
        )
        assert opp.status == Opportunity.Status.UNKNOWN.value


# =============================================================================
# Sync e2e — pipeline completa com Fakes
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestSalesSyncE2E:
    def test_bootstrap_persists_all_leads(
        self,
        organization_a: Organization,
        datasource_fake_leads_a: OrganizationDataSource,
        sample_lead_dtos: list[LeadDTO],
    ) -> None:
        FakeLeadSource.set_seed(sample_lead_dtos)

        result = sync_capability(
            organization_id=organization_a.pk, capability="LEADS", mode="BOOTSTRAP"
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        items = list(Lead.objects.all().order_by("external_id"))
        assert len(items) == 2
        assert items[0].status == "NEW"
        assert items[1].status == "CONVERTED"

    def test_bootstrap_persists_all_opportunities(
        self,
        organization_a: Organization,
        datasource_fake_opportunities_a: OrganizationDataSource,
        sample_opportunity_dtos: list[OpportunityDTO],
    ) -> None:
        FakeOpportunitySource.set_seed(sample_opportunity_dtos)

        result = sync_capability(
            organization_id=organization_a.pk,
            capability="OPPORTUNITIES",
            mode="BOOTSTRAP",
        )

        assert result["records_processed"] == 2

        set_current_organization(organization_a)
        items = list(Opportunity.objects.all().order_by("external_id"))
        assert len(items) == 2
        assert items[0].status == "OPEN"
        assert items[1].status == "WON"

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_leads_a: OrganizationDataSource,
        sample_lead_dtos: list[LeadDTO],
    ) -> None:
        FakeLeadSource.set_seed(sample_lead_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="LEADS", mode="BOOTSTRAP"
        )
        sync_capability(
            organization_id=organization_a.pk, capability="LEADS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        assert Lead.objects.count() == 2

    def test_sync_creates_job_and_checkpoint(
        self,
        organization_a: Organization,
        datasource_fake_leads_a: OrganizationDataSource,
        sample_lead_dtos: list[LeadDTO],
    ) -> None:
        FakeLeadSource.set_seed(sample_lead_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="LEADS", mode="BOOTSTRAP"
        )

        job = SyncJob.objects.filter(organization=organization_a).first()
        assert job is not None
        assert job.status == SyncStatus.COMPLETED
        assert job.records_processed == 2

        checkpoint = SyncCheckpoint.objects.get(
            organization=organization_a, source_type="FAKE", capability="LEADS"
        )
        assert checkpoint.last_processed_at is not None

    def test_resolves_lead_fk_when_leads_synced_first(
        self,
        organization_a: Organization,
        datasource_fake_leads_a: OrganizationDataSource,
        datasource_fake_opportunities_a: OrganizationDataSource,
        sample_lead_dtos: list[LeadDTO],
        sample_opportunity_dtos: list[OpportunityDTO],
    ) -> None:
        FakeLeadSource.set_seed(sample_lead_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="LEADS", mode="BOOTSTRAP"
        )
        FakeOpportunitySource.set_seed(sample_opportunity_dtos)
        sync_capability(
            organization_id=organization_a.pk,
            capability="OPPORTUNITIES",
            mode="BOOTSTRAP",
        )

        set_current_organization(organization_a)
        opp1 = Opportunity.objects.get(external_id="opp-1")
        assert opp1.lead is not None
        assert opp1.lead.external_id == "lead-1"

    def test_org_b_does_not_see_org_a_leads(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_leads_a: OrganizationDataSource,
        sample_lead_dtos: list[LeadDTO],
    ) -> None:
        FakeLeadSource.set_seed(sample_lead_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="LEADS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_b)
        assert Lead.objects.count() == 0

        set_current_organization(organization_a)
        assert Lead.objects.count() == 2
