"""Testes da reconciliação por ausência + soft-delete (fonte da verdade = IXC).

Cobre:
- `soft_delete_missing` nos repos: marca ausentes, mantém presentes, reativa os
  que voltaram, e o guard-rail que aborta pull parcial.
- Upsert reativa registro soft-deleted (deleted_at volta a None).
- Rebuild dropa as fact de registros soft-deleted (bulk_create só faz upsert).
- Reconciliação e2e de PAYMENTS: registro removido do IXC vira soft-deleted e
  some das fact, sem mexer nos demais.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.rebuild import rebuild_for_capability
from apps.analytics.infrastructure.models import FactExpense, FactPayment
from apps.financial.domain.dto import ExpenseDTO, PaymentDTO
from apps.financial.infrastructure.models import Payment
from apps.financial.infrastructure.repositories import (
    ExpenseRepository,
    PaymentRepository,
)
from apps.integrations.fake.invoices import FakePaymentSource
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.sync.tasks import reconcile_capability
from apps.tenancy.models import Organization, OrganizationDataSource


def _payment_dto(ext_id: str, amount: str = "100.00") -> PaymentDTO:
    return PaymentDTO(
        external_id=ext_id,
        invoice_external_id=None,
        contract_external_id=None,
        amount=Decimal(amount),
        paid_at=datetime(2025, 4, 8, tzinfo=UTC),
        method="PIX",
    )


def _expense_dto(ext_id: str, amount: str = "100.00", status: str = "OPEN") -> ExpenseDTO:
    from datetime import date

    return ExpenseDTO(
        external_id=ext_id,
        supplier_external_id="",
        supplier_name="Fornecedor X",
        description="despesa",
        amount=Decimal(amount),
        due_date=date(2025, 4, 10),
        status=status,
    )


def _seed_payments(org: Organization, ext_ids: list[str]) -> None:
    set_current_organization(org)
    repo = PaymentRepository(org)
    for ext in ext_ids:
        repo.upsert_from_dto(_payment_dto(ext), source_type=SourceType.FAKE)


def _seed_expenses(org: Organization, ext_ids: list[str]) -> None:
    set_current_organization(org)
    repo = ExpenseRepository(org)
    for ext in ext_ids:
        repo.upsert_from_dto(_expense_dto(ext), source_type=SourceType.FAKE)


@pytest.mark.django_db
class TestSoftDeleteMissing:
    def test_marks_absent_records(self, organization_a: Organization) -> None:
        _seed_payments(organization_a, ["pay-1", "pay-2", "pay-3", "pay-4"])
        repo = PaymentRepository(organization_a)

        result = repo.soft_delete_missing(
            seen_external_ids={"pay-1", "pay-2", "pay-3"},
            source_type=SourceType.FAKE,
        )

        assert result["aborted"] is False
        assert result["soft_deleted"] == 1
        set_current_organization(organization_a)
        assert Payment.objects.get(external_id="pay-4").deleted_at is not None
        assert Payment.objects.get(external_id="pay-1").deleted_at is None

    def test_guard_rail_aborts_partial_pull(self, organization_a: Organization) -> None:
        _seed_payments(organization_a, [f"pay-{i}" for i in range(10)])
        repo = PaymentRepository(organization_a)

        # Pull trouxe só 2 de 10 ativos (20% < 50%) → aborta sem deletar.
        result = repo.soft_delete_missing(
            seen_external_ids={"pay-0", "pay-1"},
            source_type=SourceType.FAKE,
        )

        assert result["aborted"] is True
        assert result["soft_deleted"] == 0
        set_current_organization(organization_a)
        assert Payment.objects.filter(deleted_at__isnull=True).count() == 10

    def test_reactivates_returning_record(self, organization_a: Organization) -> None:
        _seed_payments(organization_a, ["pay-1", "pay-2"])
        set_current_organization(organization_a)
        # pay-2 já estava soft-deleted de uma reconciliação anterior.
        Payment.objects.filter(external_id="pay-2").update(deleted_at=timezone.now())

        repo = PaymentRepository(organization_a)
        result = repo.soft_delete_missing(
            seen_external_ids={"pay-1", "pay-2"},  # pay-2 voltou ao IXC
            source_type=SourceType.FAKE,
        )

        assert result["reactivated"] == 1
        assert Payment.objects.get(external_id="pay-2").deleted_at is None

    def test_upsert_reactivates_soft_deleted(self, organization_a: Organization) -> None:
        _seed_payments(organization_a, ["pay-1"])
        set_current_organization(organization_a)
        Payment.objects.filter(external_id="pay-1").update(deleted_at=timezone.now())

        PaymentRepository(organization_a).upsert_from_dto(
            _payment_dto("pay-1"), source_type=SourceType.FAKE
        )
        assert Payment.objects.get(external_id="pay-1").deleted_at is None


@pytest.mark.django_db
class TestRebuildDropsSoftDeleted:
    def test_payment_fact_dropped_when_soft_deleted(
        self, organization_a: Organization
    ) -> None:
        _seed_payments(organization_a, ["pay-1", "pay-2"])
        rebuild_for_capability(organization_a, "PAYMENTS")
        set_current_organization(organization_a)
        assert FactPayment.objects.filter(organization=organization_a).count() == 2

        Payment.objects.filter(external_id="pay-2").update(deleted_at=timezone.now())
        rebuild_for_capability(organization_a, "PAYMENTS")

        facts = list(
            FactPayment.objects.filter(organization=organization_a)
            .values_list("payment__external_id", flat=True)
        )
        assert facts == ["pay-1"]

    def test_expense_fact_dropped_when_soft_deleted(
        self, organization_a: Organization
    ) -> None:
        _seed_expenses(organization_a, ["exp-1", "exp-2", "exp-3"])
        repo = ExpenseRepository(organization_a)
        result = repo.soft_delete_missing(
            seen_external_ids={"exp-1", "exp-2"},
            source_type=SourceType.FAKE,
        )
        assert result["soft_deleted"] == 1

        rebuild_for_capability(organization_a, "EXPENSES")
        set_current_organization(organization_a)
        kept = set(
            FactExpense.objects.filter(organization=organization_a)
            .values_list("expense__external_id", flat=True)
        )
        assert kept == {"exp-1", "exp-2"}


@pytest.mark.django_db
class TestReconciliationE2E:
    def test_removed_payment_is_soft_deleted_and_drops_fact(
        self,
        organization_a: Organization,
        datasource_fake_payments_a: OrganizationDataSource,
    ) -> None:
        # Estado inicial: 2 pagamentos vindos do IXC + fact materializada.
        _seed_payments(organization_a, ["pay-1", "pay-2"])
        rebuild_for_capability(organization_a, "PAYMENTS")

        # IXC agora só tem pay-1 (pay-2 foi removido lá — carnê resolvido).
        FakePaymentSource.set_seed([_payment_dto("pay-1")])

        reconcile_capability(
            organization_id=organization_a.pk, capability="PAYMENTS"
        )

        set_current_organization(organization_a)
        assert Payment.objects.get(external_id="pay-2").deleted_at is not None
        assert Payment.objects.get(external_id="pay-1").deleted_at is None
        # rebuild disparado pelo sync_completed dropou a fact do soft-deleted.
        facts = list(
            FactPayment.objects.filter(organization=organization_a)
            .values_list("payment__external_id", flat=True)
        )
        assert facts == ["pay-1"]

    def test_org_isolation_on_reconcile(
        self,
        organization_a: Organization,
        organization_b: Organization,
        datasource_fake_payments_a: OrganizationDataSource,
    ) -> None:
        _seed_payments(organization_a, ["pay-1", "pay-2"])
        _seed_payments(organization_b, ["pay-1", "pay-2"])

        FakePaymentSource.set_seed([_payment_dto("pay-1")])
        reconcile_capability(
            organization_id=organization_a.pk, capability="PAYMENTS"
        )

        set_current_organization(organization_b)
        # Org B não tem datasource desse fake → não reconcilia; nada soft-deleted.
        assert Payment.objects.filter(deleted_at__isnull=True).count() == 2
