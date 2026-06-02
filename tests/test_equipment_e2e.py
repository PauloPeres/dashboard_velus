"""Testes e2e de inventory — EquipmentRepository + sync ponta a ponta com FakeEquipmentSource.

Roda toda a pipeline: registry → adapter → DTO → repository → DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.equipment import FakeEquipmentSource
from apps.integrations.shared.enums import SourceType
from apps.inventory.domain.dto import EquipmentDTO
from apps.inventory.infrastructure.models import ContractEquipment
from apps.inventory.infrastructure.repositories import EquipmentRepository
from apps.shared.context import set_current_organization
from apps.sync.models import SyncCheckpoint, SyncJob, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


# =============================================================================
# EquipmentRepository — upsert idempotente, FK, normalização
# =============================================================================
@pytest.mark.django_db
class TestEquipmentRepository:
    def _dto(self, **overrides: object) -> EquipmentDTO:
        base = {
            "external_id": "eq-1",
            "contract_external_id": "ctr-1",
            "product_name": "ONT Huawei HG8245",
            "status": "ACTIVE",
            "serial": "SN-0001",
            "mac": "AA:BB:CC:00:00:01",
            "value": Decimal("250.00"),
        }
        base.update(overrides)
        return EquipmentDTO(**base)  # type: ignore[arg-type]

    def test_creates_equipment(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = EquipmentRepository(organization_a)
        eq, created = repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        assert created is True
        assert eq.external_id == "eq-1"
        assert eq.status == "ACTIVE"
        assert eq.product_name == "ONT Huawei HG8245"
        assert eq.value == Decimal("250.00")

    def test_upsert_idempotent_no_duplicate(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = EquipmentRepository(organization_a)
        repo.upsert_from_dto(self._dto(), source_type=SourceType.IXC)
        _, created = repo.upsert_from_dto(
            self._dto(status="RETURNED"), source_type=SourceType.IXC
        )
        assert created is False

        set_current_organization(organization_a)
        assert ContractEquipment.objects.count() == 1
        assert ContractEquipment.objects.get(external_id="eq-1").status == "RETURNED"

    def test_resolves_contract_fk(
        self,
        organization_a: Organization,
        datasource_fake_contracts_a: OrganizationDataSource,
    ) -> None:
        FakeContractSource.set_seed([
            ContractDTO(
                external_id="ctr-1",
                customer_external_id="ext-1",
                plan_name="Fibra 500M",
                monthly_amount=Decimal("150.00"),
                status="ACTIVE",
                activated_at=datetime(2025, 1, 15, tzinfo=UTC),
            )
        ])
        sync_capability(
            organization_id=organization_a.pk, capability="CONTRACTS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        repo = EquipmentRepository(organization_a)
        eq, _ = repo.upsert_from_dto(self._dto(), source_type=SourceType.FAKE)
        assert eq.contract is not None
        assert eq.contract.external_id == "ctr-1"

    def test_persists_with_null_fk_when_contract_absent(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        repo = EquipmentRepository(organization_a)
        eq, _ = repo.upsert_from_dto(
            self._dto(contract_external_id="ghost-999"), source_type=SourceType.IXC
        )
        assert eq.contract is None
        assert eq.contract_external_id == "ghost-999"

    def test_unknown_status_normalized(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        repo = EquipmentRepository(organization_a)
        eq, _ = repo.upsert_from_dto(
            self._dto(status="WHATEVER"), source_type=SourceType.IXC
        )
        assert eq.status == ContractEquipment.Status.UNKNOWN.value


# =============================================================================
# Sync e2e — pipeline completa com FakeEquipmentSource
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestEquipmentSyncE2E:
    def test_bootstrap_persists_all_equipment(
        self,
        organization_a: Organization,
        datasource_fake_equipment_a: OrganizationDataSource,
        sample_equipment_dtos: list[EquipmentDTO],
    ) -> None:
        FakeEquipmentSource.set_seed(sample_equipment_dtos)

        result = sync_capability(
            organization_id=organization_a.pk, capability="EQUIPMENT", mode="BOOTSTRAP"
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        items = list(ContractEquipment.objects.all().order_by("external_id"))
        assert len(items) == 2
        assert items[0].status == "ACTIVE"
        assert items[1].status == "RETURNED"

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_equipment_a: OrganizationDataSource,
        sample_equipment_dtos: list[EquipmentDTO],
    ) -> None:
        FakeEquipmentSource.set_seed(sample_equipment_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="EQUIPMENT", mode="BOOTSTRAP"
        )
        sync_capability(
            organization_id=organization_a.pk, capability="EQUIPMENT", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        assert ContractEquipment.objects.count() == 2

    def test_sync_creates_job_and_checkpoint(
        self,
        organization_a: Organization,
        datasource_fake_equipment_a: OrganizationDataSource,
        sample_equipment_dtos: list[EquipmentDTO],
    ) -> None:
        FakeEquipmentSource.set_seed(sample_equipment_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="EQUIPMENT", mode="BOOTSTRAP"
        )

        job = SyncJob.objects.filter(organization=organization_a).first()
        assert job is not None
        assert job.status == SyncStatus.COMPLETED
        assert job.records_processed == 2

        checkpoint = SyncCheckpoint.objects.get(
            organization=organization_a, source_type="FAKE", capability="EQUIPMENT"
        )
        assert checkpoint.last_processed_at is not None

    def test_resolves_contract_fk_when_contracts_synced_first(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
        datasource_fake_equipment_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
        sample_contract_dtos: list[ContractDTO],
        sample_equipment_dtos: list[EquipmentDTO],
    ) -> None:
        FakeCustomerSource.set_seed(sample_customer_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CUSTOMERS", mode="BOOTSTRAP"
        )
        FakeContractSource.set_seed(sample_contract_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="CONTRACTS", mode="BOOTSTRAP"
        )
        FakeEquipmentSource.set_seed(sample_equipment_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="EQUIPMENT", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        eq1 = ContractEquipment.objects.get(external_id="eq-1")
        assert eq1.contract is not None
        assert eq1.contract.external_id == "ctr-1"

    def test_org_b_does_not_see_org_a_equipment(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_equipment_a: OrganizationDataSource,
        sample_equipment_dtos: list[EquipmentDTO],
    ) -> None:
        FakeEquipmentSource.set_seed(sample_equipment_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="EQUIPMENT", mode="BOOTSTRAP"
        )

        set_current_organization(organization_b)
        assert ContractEquipment.objects.count() == 0

        set_current_organization(organization_a)
        assert ContractEquipment.objects.count() == 2
