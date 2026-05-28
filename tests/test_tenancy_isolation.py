"""Testes obrigatórios de isolamento entre tenants.

AGENT.md §3.3 (regra de ouro): todo model que herda TenantModel deve ter teste
provando que org_a NÃO vê dados de org_b. Aqui pra Customer; ao adicionar
novos models de domínio (Contract, Invoice, ...), replicar o padrão.
"""

from __future__ import annotations

import pytest

from apps.customers.infrastructure.models import Customer
from apps.shared.context import is_cross_tenant_allowed, set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.shared.exceptions import NoOrganizationInContextError
from apps.tenancy.models import Organization


def _save_customer(**fields: object) -> Customer:
    """Helper de setup: instancia + save direto.

    Evita Customer.objects.create() que passa pelo TenantManager.get_queryset()
    e exigiria contextvar setado. Em produção, criação passa pelo Repository
    dentro de @allow_cross_tenant do sync orchestrator.
    """
    customer = Customer(**fields)
    customer.save()
    return customer


@pytest.mark.django_db
class TestTenantManagerEnforcement:
    """O TenantManager precisa SEMPRE filtrar por contextvar."""

    def test_raises_when_no_organization_in_context(
        self, organization_a: Organization
    ) -> None:
        """Query sem org no contexto levanta exceção — defesa principal."""
        _save_customer(
            organization=organization_a,
            source_type="FAKE",
            external_id="x-1",
            document="11111111111",
            name="Cliente A",
            status="ACTIVE",
        )
        # Sem set_current_organization → leitura deve falhar
        with pytest.raises(NoOrganizationInContextError):
            list(Customer.objects.all())

    def test_filters_by_current_organization(
        self,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        """Org_a só vê seus dados, org_b só os seus."""
        _save_customer(
            organization=organization_a,
            source_type="FAKE",
            external_id="a-1",
            document="11111111111",
            name="Cliente A",
            status="ACTIVE",
        )
        _save_customer(
            organization=organization_b,
            source_type="FAKE",
            external_id="b-1",
            document="22222222222",
            name="Cliente B",
            status="ACTIVE",
        )

        set_current_organization(organization_a)
        a_customers = list(Customer.objects.all())
        assert len(a_customers) == 1
        assert a_customers[0].name == "Cliente A"

        set_current_organization(organization_b)
        b_customers = list(Customer.objects.all())
        assert len(b_customers) == 1
        assert b_customers[0].name == "Cliente B"

    def test_allow_cross_tenant_decorator_bypasses_filter(
        self,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        """@allow_cross_tenant permite ver todos os tenants — auditado."""
        _save_customer(
            organization=organization_a,
            source_type="FAKE",
            external_id="a-1",
            document="11111111111",
            name="Cliente A",
            status="ACTIVE",
        )
        _save_customer(
            organization=organization_b,
            source_type="FAKE",
            external_id="b-1",
            document="22222222222",
            name="Cliente B",
            status="ACTIVE",
        )

        @allow_cross_tenant(reason="test: contagem global")
        def count_all() -> int:
            assert is_cross_tenant_allowed() is True
            return Customer.objects.count()

        assert count_all() == 2
        assert is_cross_tenant_allowed() is False  # flag restaurada após decorator

    def test_no_cross_tenant_leakage_after_context_switch(
        self,
        organization_a: Organization,
        organization_b: Organization,
    ) -> None:
        """Switch de contexto não deixa estado residual entre orgs."""
        _save_customer(
            organization=organization_a,
            source_type="FAKE",
            external_id="a-1",
            document="11111111111",
            name="A",
            status="ACTIVE",
        )

        set_current_organization(organization_a)
        assert Customer.objects.count() == 1

        set_current_organization(organization_b)
        assert Customer.objects.count() == 0  # Org B não tem nada

        set_current_organization(organization_a)
        assert Customer.objects.count() == 1  # Volta a ver os de A
