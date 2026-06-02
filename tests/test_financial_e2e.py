"""Testes e2e da Fase 0.B — Contracts + Invoices + Payments sync ponta a ponta."""

from __future__ import annotations

import pytest

from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.customers.infrastructure.models import Contract, Customer
from apps.financial.domain.dto import InvoiceDTO
from apps.financial.infrastructure.models import Invoice, Payment
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.invoices import FakeInvoiceSource
from apps.shared.context import set_current_organization
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource


@pytest.mark.django_db
@pytest.mark.e2e
class TestContractsSync:
    def test_bootstrap_persists_contracts_with_customer_fk(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
        sample_contract_dtos: list[ContractDTO],
    ) -> None:
        # Sync customers PRIMEIRO (Contracts dependem deles)
        FakeCustomerSource.set_seed(sample_customer_dtos)
        sync_capability(
            organization_id=organization_a.pk,
            capability="CUSTOMERS",
            mode="BOOTSTRAP",
        )

        # Depois contracts — FK resolve
        FakeContractSource.set_seed(sample_contract_dtos)
        result = sync_capability(
            organization_id=organization_a.pk,
            capability="CONTRACTS",
            mode="BOOTSTRAP",
        )

        assert result["records_processed"] == 2
        assert result["sources"][0]["status"] == "OK"

        set_current_organization(organization_a)
        contracts = list(Contract.objects.all().order_by("external_id"))
        assert len(contracts) == 2
        assert contracts[0].customer is not None  # FK resolveu
        assert contracts[0].customer.name == "Cliente Um"
        assert contracts[0].plan_name == "Fibra 500M"

    def test_contracts_without_customer_persist_with_null_fk(
        self,
        organization_a: Organization,
        datasource_fake_contracts_a: OrganizationDataSource,
        sample_contract_dtos: list[ContractDTO],
    ) -> None:
        """Contracts chegando ANTES dos Customers — persiste com FK=NULL."""
        FakeContractSource.set_seed(sample_contract_dtos)
        sync_capability(
            organization_id=organization_a.pk,
            capability="CONTRACTS",
            mode="BOOTSTRAP",
        )

        set_current_organization(organization_a)
        contracts = list(Contract.objects.all())
        assert len(contracts) == 2
        assert all(c.customer is None for c in contracts)
        # Snapshot do customer_external_id preservado
        assert contracts[0].customer_external_id in ("ext-1", "ext-2")


@pytest.mark.django_db
@pytest.mark.e2e
class TestInvoicesSync:
    def test_invoices_resolve_contract_fk(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
        datasource_fake_invoices_a: OrganizationDataSource,
        sample_customer_dtos: list[CustomerDTO],
        sample_contract_dtos: list[ContractDTO],
        sample_invoice_dtos: list[InvoiceDTO],
    ) -> None:
        # Pipeline: Customers → Contracts → Invoices
        FakeCustomerSource.set_seed(sample_customer_dtos)
        sync_capability(organization_id=organization_a.pk, capability="CUSTOMERS", mode="BOOTSTRAP")

        FakeContractSource.set_seed(sample_contract_dtos)
        sync_capability(organization_id=organization_a.pk, capability="CONTRACTS", mode="BOOTSTRAP")

        FakeInvoiceSource.set_seed(sample_invoice_dtos)
        result = sync_capability(
            organization_id=organization_a.pk,
            capability="INVOICES",
            mode="BOOTSTRAP",
        )

        assert result["records_processed"] == 2

        set_current_organization(organization_a)
        invoices = list(Invoice.objects.all().order_by("external_id"))
        assert len(invoices) == 2
        # FK resolveu pro Contract correto
        assert invoices[0].contract is not None
        assert invoices[0].contract.external_id == "ctr-1"
        assert invoices[0].status == "PAID"
        assert invoices[1].status == "PENDING"


@pytest.mark.django_db
class TestFinancialCrossTenant:
    """Isolamento dos novos models entre tenants."""

    def test_invoice_isolated_per_tenant(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        from datetime import date
        from decimal import Decimal

        # Cria customer + contract base pra ter FK válida
        cust_a = Customer(
            organization=organization_a,
            source_type="FAKE", external_id="c-a-1",
            document="111", name="A", status="ACTIVE",
        )
        cust_a.save()
        ctr_a = Contract(
            organization=organization_a,
            source_type="FAKE", external_id="ct-a-1",
            customer=cust_a, customer_external_id="c-a-1",
            plan_name="P", monthly_amount=Decimal("100"), status="ACTIVE",
        )
        ctr_a.save()
        Invoice(
            organization=organization_a,
            source_type="FAKE", external_id="i-a-1",
            contract=ctr_a, contract_external_id="ct-a-1",
            amount=Decimal("100"), due_date=date(2025, 5, 1), status="PENDING",
        ).save()

        # Org B tem 0 invoices
        set_current_organization(organization_a)
        assert Invoice.objects.count() == 1

        set_current_organization(organization_b)
        assert Invoice.objects.count() == 0

    def test_payment_isolated_per_tenant(
        self, organization_a: Organization, organization_b: Organization
    ) -> None:
        from datetime import UTC, datetime
        from decimal import Decimal

        Payment(
            organization=organization_a,
            source_type="FAKE", external_id="p-a-1",
            amount=Decimal("50"), paid_at=datetime(2025, 5, 1, tzinfo=UTC),
            method="PIX",
        ).save()

        set_current_organization(organization_a)
        assert Payment.objects.count() == 1

        set_current_organization(organization_b)
        assert Payment.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.e2e
class TestPaymentSync:
    def test_bootstrap_persists_payments(
        self,
        organization_a: Organization,
        datasource_fake_payments_a: OrganizationDataSource,
        sample_payment_dtos: list,
    ) -> None:
        from apps.integrations.fake.invoices import FakePaymentSource

        FakePaymentSource.set_seed(sample_payment_dtos)
        result = sync_capability(
            organization_id=organization_a.pk, capability="PAYMENTS", mode="BOOTSTRAP"
        )

        assert result["records_processed"] == 1
        set_current_organization(organization_a)
        assert Payment.objects.count() == 1
        pay = Payment.objects.first()
        assert pay.external_id == "pay-1"
        assert pay.method == "PIX"

    def test_idempotency_no_duplicates_on_rerun(
        self,
        organization_a: Organization,
        datasource_fake_payments_a: OrganizationDataSource,
        sample_payment_dtos: list,
    ) -> None:
        from apps.integrations.fake.invoices import FakePaymentSource

        FakePaymentSource.set_seed(sample_payment_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="PAYMENTS", mode="BOOTSTRAP"
        )
        sync_capability(
            organization_id=organization_a.pk, capability="PAYMENTS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        assert Payment.objects.count() == 1

    def test_resolves_invoice_fk_when_invoices_synced_first(
        self,
        organization_a: Organization,
        datasource_fake_invoices_a: OrganizationDataSource,
        datasource_fake_payments_a: OrganizationDataSource,
        sample_invoice_dtos: list,
        sample_payment_dtos: list,
    ) -> None:
        from apps.integrations.fake.invoices import FakeInvoiceSource, FakePaymentSource

        FakeInvoiceSource.set_seed(sample_invoice_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="INVOICES", mode="BOOTSTRAP"
        )
        FakePaymentSource.set_seed(sample_payment_dtos)
        sync_capability(
            organization_id=organization_a.pk, capability="PAYMENTS", mode="BOOTSTRAP"
        )

        set_current_organization(organization_a)
        pay = Payment.objects.get(external_id="pay-1")
        assert pay.invoice is not None
        assert pay.invoice.external_id == "inv-1"
