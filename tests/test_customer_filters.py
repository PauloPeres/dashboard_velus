"""Testes dos filtros de segmentação de `search_customers` (#36).

`search_customers` ganhou filtros combináveis (status, risco de churn, faixa de
MRR, inadimplência, equipamento em campo, chamado recente) além da busca textual.
Os filtros booleanos usam EXISTS (subquery) e o MRR usa subquery agregada — aqui
cobrimos cada filtro isolado, o caso "NONE" de risco e combinações.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import search_customers
from apps.analytics.infrastructure.models import ChurnRiskScore
from apps.customers.infrastructure.models import Contract, Customer
from apps.financial.infrastructure.models import Invoice
from apps.helpdesk.infrastructure.models import Ticket
from apps.inventory.infrastructure.models import ContractEquipment
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _customer(org: Organization, name: str, *, status: str = "ACTIVE") -> Customer:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Customer.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-cust-{_seq}",
        name=name,
        document=f"doc-{_seq}",
        status=status,
    )


def _contract(
    org: Organization,
    customer: Customer,
    *,
    monthly: Decimal,
    status: str = "ACTIVE",
    addons: Decimal = Decimal("0"),
    discounts: Decimal = Decimal("0"),
) -> Contract:
    global _seq
    _seq += 1
    set_current_organization(org)
    return Contract.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-ctr-{_seq}",
        customer=customer,
        customer_external_id=customer.external_id,
        plan_name="Plano X",
        monthly_amount=monthly,
        monthly_amount_addons=addons,
        monthly_amount_discounts=discounts,
        status=status,
    )


def _risk(org: Organization, customer: Customer, level: str) -> None:
    set_current_organization(org)
    ChurnRiskScore.objects.create(
        organization=org,
        customer=customer,
        level=level,
        score=50,
        computed_at=timezone.now(),
    )


def _overdue_invoice(org: Organization, contract: Contract) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    Invoice.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-inv-{_seq}",
        contract=contract,
        contract_external_id=contract.external_id,
        amount=Decimal("100"),
        due_date=timezone.now().date() - timedelta(days=10),
        status="OVERDUE",
    )


def _equipment(org: Organization, contract: Contract, *, status: str = "ACTIVE") -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    ContractEquipment.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-eq-{_seq}",
        contract=contract,
        contract_external_id=contract.external_id,
        status=status,
    )


def _ticket(org: Organization, customer: Customer, *, days_ago: int) -> None:
    global _seq
    _seq += 1
    set_current_organization(org)
    Ticket.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"cf-tk-{_seq}",
        customer=customer,
        customer_external_id=customer.external_id,
        status="OPEN",
        opened_at=timezone.now() - timedelta(days=days_ago),
    )


def _names(results: list[dict]) -> set[str]:
    return {r["name"] for r in results}


@pytest.mark.django_db
class TestStatusFilter:
    def test_filters_by_status(self, organization_a: Organization) -> None:
        _customer(organization_a, "Ativo", status="ACTIVE")
        _customer(organization_a, "Bloqueado", status="BLOCKED")
        set_current_organization(organization_a)

        results = search_customers(organization_a, status="BLOCKED")
        assert _names(results) == {"Bloqueado"}


@pytest.mark.django_db
class TestMrrFilter:
    def test_mrr_range_uses_net_amount(self, organization_a: Organization) -> None:
        # MRR líquido = monthly + addons - discounts.
        low = _customer(organization_a, "Baixo")
        _contract(organization_a, low, monthly=Decimal("50"))
        high = _customer(organization_a, "Alto")
        _contract(
            organization_a,
            high,
            monthly=Decimal("100"),
            addons=Decimal("60"),
            discounts=Decimal("10"),
        )  # net = 150
        set_current_organization(organization_a)

        assert _names(search_customers(organization_a, mrr_min=100)) == {"Alto"}
        assert _names(search_customers(organization_a, mrr_max=80)) == {"Baixo"}
        assert _names(
            search_customers(organization_a, mrr_min=100, mrr_max=200)
        ) == {"Alto"}

    def test_only_active_contracts_count_for_mrr(
        self, organization_a: Organization
    ) -> None:
        c = _customer(organization_a, "SoCancelado")
        _contract(organization_a, c, monthly=Decimal("500"), status="CANCELED")
        set_current_organization(organization_a)

        # Contrato cancelado não soma MRR → cliente fica com MRR 0.
        assert _names(search_customers(organization_a, mrr_min=1)) == set()


@pytest.mark.django_db
class TestRiskFilter:
    def test_filters_by_level(self, organization_a: Organization) -> None:
        h = _customer(organization_a, "AltoRisco")
        _risk(organization_a, h, ChurnRiskScore.LEVEL_HIGH)
        m = _customer(organization_a, "MedioRisco")
        _risk(organization_a, m, ChurnRiskScore.LEVEL_MEDIUM)
        set_current_organization(organization_a)

        results = search_customers(organization_a, risk_level="HIGH")
        assert _names(results) == {"AltoRisco"}
        assert results[0]["risk_level"] == "HIGH"

    def test_none_returns_customers_without_score(
        self, organization_a: Organization
    ) -> None:
        scored = _customer(organization_a, "ComScore")
        _risk(organization_a, scored, ChurnRiskScore.LEVEL_LOW)
        _customer(organization_a, "SemScore")
        set_current_organization(organization_a)

        assert _names(search_customers(organization_a, risk_level="NONE")) == {
            "SemScore"
        }


@pytest.mark.django_db
class TestOverdueFilter:
    def test_only_customers_with_overdue_invoice(
        self, organization_a: Organization
    ) -> None:
        inad = _customer(organization_a, "Inadimplente")
        ctr = _contract(organization_a, inad, monthly=Decimal("100"))
        _overdue_invoice(organization_a, ctr)
        _customer(organization_a, "EmDia")
        set_current_organization(organization_a)

        assert _names(search_customers(organization_a, overdue=True)) == {
            "Inadimplente"
        }


@pytest.mark.django_db
class TestEquipmentFilter:
    def test_only_customers_with_active_equipment(
        self, organization_a: Organization
    ) -> None:
        com = _customer(organization_a, "ComEquip")
        ctr = _contract(organization_a, com, monthly=Decimal("100"))
        _equipment(organization_a, ctr, status="ACTIVE")
        sem = _customer(organization_a, "EquipDevolvido")
        ctr2 = _contract(organization_a, sem, monthly=Decimal("100"))
        _equipment(organization_a, ctr2, status="RETURNED")
        set_current_organization(organization_a)

        assert _names(search_customers(organization_a, has_equipment=True)) == {
            "ComEquip"
        }


@pytest.mark.django_db
class TestRecentTicketFilter:
    def test_only_customers_with_recent_ticket(
        self, organization_a: Organization
    ) -> None:
        recente = _customer(organization_a, "ChamouOntem")
        _ticket(organization_a, recente, days_ago=1)
        antigo = _customer(organization_a, "ChamouAnoPassado")
        _ticket(organization_a, antigo, days_ago=200)
        set_current_organization(organization_a)

        assert _names(
            search_customers(organization_a, recent_ticket_days=30)
        ) == {"ChamouOntem"}


@pytest.mark.django_db
class TestCombinedFilters:
    def test_status_and_overdue_combine(self, organization_a: Organization) -> None:
        # Alvo: ativo E inadimplente.
        alvo = _customer(organization_a, "AtivoInadimplente", status="ACTIVE")
        ctr = _contract(organization_a, alvo, monthly=Decimal("100"))
        _overdue_invoice(organization_a, ctr)
        # Inadimplente mas bloqueado — filtrado pelo status.
        outro = _customer(organization_a, "BloqueadoInadimplente", status="BLOCKED")
        ctr2 = _contract(organization_a, outro, monthly=Decimal("100"))
        _overdue_invoice(organization_a, ctr2)
        # Ativo mas em dia — filtrado pelo overdue.
        _customer(organization_a, "AtivoEmDia", status="ACTIVE")
        set_current_organization(organization_a)

        results = search_customers(
            organization_a, status="ACTIVE", overdue=True
        )
        assert _names(results) == {"AtivoInadimplente"}

    def test_query_plus_filter(self, organization_a: Organization) -> None:
        match = _customer(organization_a, "Joao Silva")
        _risk(organization_a, match, ChurnRiskScore.LEVEL_HIGH)
        other = _customer(organization_a, "Joao Souza")
        _risk(organization_a, other, ChurnRiskScore.LEVEL_LOW)
        set_current_organization(organization_a)

        results = search_customers(
            organization_a, query="Joao", risk_level="HIGH"
        )
        assert _names(results) == {"Joao Silva"}
