"""Testes do sistema de predição de churn — engine de scoring + aggregations + views.

Cobre `compute_churn_risk_scores` (sinais: bloqueio prolongado, atraso recorrente,
chamados frequentes, offline), `compute_churn_risk_summary`,
`compute_top_risk_customers` e a view /risk/ + alerta no executivo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_churn_risk_summary,
    compute_top_risk_customers,
)
from apps.analytics.application.churn_risk import compute_churn_risk_scores
from apps.analytics.infrastructure.models import ChurnRiskScore
from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.financial.domain.dto import InvoiceDTO
from apps.helpdesk.domain.dto import TicketDTO
from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.invoices import FakeInvoiceSource
from apps.integrations.fake.tickets import FakeTicketSource
from apps.network.domain.dto import ConnectionDTO
from apps.shared.context import set_current_organization
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource, User


def _sync(org: Organization, capability: str) -> None:
    sync_capability(organization_id=org.pk, capability=capability, mode="BOOTSTRAP")


def _signal_codes(score: ChurnRiskScore) -> set[str]:
    return {s["code"] for s in score.signals}


# =============================================================================
# Cenário sintético — datas relativas a "hoje" pra cair nas janelas dos sinais
# =============================================================================
@pytest.fixture
def churn_scenario(
    organization_a: Organization,
    datasource_fake_customers_a: OrganizationDataSource,
    datasource_fake_contracts_a: OrganizationDataSource,
    datasource_fake_invoices_a: OrganizationDataSource,
    datasource_fake_tickets_a: OrganizationDataSource,
    datasource_fake_connections_a: OrganizationDataSource,
) -> Organization:
    """Seed de 3 clientes com perfis de risco distintos.

    - ext-high   : ACTIVE + 3 faturas vencidas + 3 chamados recentes + offline → HIGH (60)
    - ext-blocked: contrato BLOCKED (sem snapshot prévio → 999 dias)           → MEDIUM (40)
    - ext-clean  : ACTIVE, online, sem atraso/chamados                          → sem score
    """
    now = timezone.now()
    today = now.date()

    customers = [
        CustomerDTO(external_id="ext-high", document="11111111111",
                    name="Cliente Alto Risco", status="ACTIVE",
                    created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        CustomerDTO(external_id="ext-blocked", document="22222222222",
                    name="Cliente Bloqueado", status="BLOCKED",
                    created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        CustomerDTO(external_id="ext-clean", document="33333333333",
                    name="Cliente Saudável", status="ACTIVE",
                    created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
    ]
    contracts = [
        ContractDTO(external_id="ctr-high", customer_external_id="ext-high",
                    plan_name="Fibra 1G", monthly_amount=Decimal("200.00"),
                    status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ContractDTO(external_id="ctr-blocked", customer_external_id="ext-blocked",
                    plan_name="Fibra 200M", monthly_amount=Decimal("100.00"),
                    status="BLOCKED", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ContractDTO(external_id="ctr-clean", customer_external_id="ext-clean",
                    plan_name="Fibra 500M", monthly_amount=Decimal("150.00"),
                    status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
    ]
    # 3 faturas vencidas pro ext-high (dentro de 6 meses) + 1 paga pro ext-clean
    invoices = [
        InvoiceDTO(external_id="inv-h1", contract_external_id="ctr-high",
                   amount=Decimal("200.00"), due_date=today - timedelta(days=10),
                   status="PENDING"),
        InvoiceDTO(external_id="inv-h2", contract_external_id="ctr-high",
                   amount=Decimal("200.00"), due_date=today - timedelta(days=40),
                   status="PENDING"),
        InvoiceDTO(external_id="inv-h3", contract_external_id="ctr-high",
                   amount=Decimal("200.00"), due_date=today - timedelta(days=70),
                   status="PENDING"),
        InvoiceDTO(external_id="inv-c1", contract_external_id="ctr-clean",
                   amount=Decimal("150.00"), due_date=today - timedelta(days=5),
                   status="PAID", paid_at=now, paid_amount=Decimal("150.00")),
    ]
    # 3 chamados recentes pro ext-high
    tickets = [
        TicketDTO(external_id=f"tk-h{i}", customer_external_id="ext-high",
                  subject_id="1", sector="Suporte", technician_id="",
                  status="OPEN", priority="HIGH", message="Instabilidade",
                  protocol=f"P{i}", opened_at=now - timedelta(days=i))
        for i in (1, 5, 10)
    ]
    connections = [
        ConnectionDTO(external_id="conn-high", customer_external_id="ext-high",
                      contract_external_id="ctr-high", login="high",
                      status="OFFLINE", ip="", nas_ip="10.0.0.1",
                      rx_bytes=0, tx_bytes=0, last_connection_at=now - timedelta(days=2)),
        ConnectionDTO(external_id="conn-clean", customer_external_id="ext-clean",
                      contract_external_id="ctr-clean", login="clean",
                      status="ONLINE", ip="10.0.0.2", nas_ip="10.0.0.1",
                      rx_bytes=1_000, tx_bytes=1_000, last_connection_at=now),
    ]

    FakeCustomerSource.set_seed(customers)
    _sync(organization_a, "CUSTOMERS")
    FakeContractSource.set_seed(contracts)
    _sync(organization_a, "CONTRACTS")
    FakeInvoiceSource.set_seed(invoices)
    _sync(organization_a, "INVOICES")
    FakeTicketSource.set_seed(tickets)
    _sync(organization_a, "TICKETS")
    FakeConnectionSource.set_seed(connections)
    _sync(organization_a, "CONNECTIONS")
    return organization_a


def _scores_by_external_id(org: Organization) -> dict[str, ChurnRiskScore]:
    set_current_organization(org)
    return {
        s.customer.external_id: s
        for s in ChurnRiskScore.objects.select_related("customer").all()
    }


# =============================================================================
# Engine de scoring
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestComputeChurnRiskScores:
    def test_summary_counts(self, churn_scenario: Organization) -> None:
        summary = compute_churn_risk_scores(churn_scenario)
        assert summary["at_risk"] == 2  # high + blocked; clean não pontua
        assert summary["high"] == 1
        assert summary["medium"] == 1
        assert summary["low"] == 0

    def test_clean_customer_has_no_score(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        scores = _scores_by_external_id(churn_scenario)
        assert "ext-clean" not in scores

    def test_high_risk_signals(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        score = _scores_by_external_id(churn_scenario)["ext-high"]
        assert score.level == ChurnRiskScore.LEVEL_HIGH
        assert score.score == 60  # 25 + 20 + 15
        assert _signal_codes(score) == {"LATE_PAYMENTS", "FREQUENT_TICKETS", "OFFLINE"}
        # receita em risco = mensalidade líquida do contrato ativo
        assert float(score.monthly_amount) == 200.0

    def test_blocked_signal(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        score = _scores_by_external_id(churn_scenario)["ext-blocked"]
        assert score.level == ChurnRiskScore.LEVEL_MEDIUM
        assert score.score == 40
        assert _signal_codes(score) == {"CONTRACT_BLOCKED"}
        # sinal interno _days não vaza pro JSON persistido
        assert all("_days" not in s for s in score.signals)

    def test_signals_sorted_by_weight_desc(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        score = _scores_by_external_id(churn_scenario)["ext-high"]
        weights = [s["weight"] for s in score.signals]
        assert weights == sorted(weights, reverse=True)

    def test_idempotent(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        compute_churn_risk_scores(churn_scenario)
        set_current_organization(churn_scenario)
        # exatamente 1 linha por cliente em risco, mesmo após reexecução
        assert ChurnRiskScore.objects.count() == 2

    def test_deletes_stale_scores(self, churn_scenario: Organization) -> None:
        from apps.customers.infrastructure.models import Contract

        compute_churn_risk_scores(churn_scenario)
        set_current_organization(churn_scenario)
        assert ChurnRiskScore.objects.filter(customer__external_id="ext-blocked").exists()

        # cliente sai do risco: contrato desbloqueado → engine remove a linha
        Contract.objects.filter(
            organization=churn_scenario, external_id="ctr-blocked"
        ).update(status="ACTIVE")
        compute_churn_risk_scores(churn_scenario)
        assert not ChurnRiskScore.objects.filter(
            customer__external_id="ext-blocked"
        ).exists()

    def test_isolated_per_org(
        self, churn_scenario: Organization, organization_b: Organization
    ) -> None:
        compute_churn_risk_scores(churn_scenario)
        compute_churn_risk_scores(organization_b)
        set_current_organization(organization_b)
        assert ChurnRiskScore.objects.count() == 0


# =============================================================================
# Aggregations
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestChurnRiskAggregations:
    def test_summary(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        summary = compute_churn_risk_summary(churn_scenario)
        assert summary["total_at_risk"] == 2
        assert summary["high"] == 1
        assert summary["medium"] == 1
        # receita em risco = high (200) + medium (100)
        assert summary["revenue_at_risk"] == 300.0
        assert summary["computed_at"] is not None

    def test_signal_distribution(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        summary = compute_churn_risk_summary(churn_scenario)
        counts = {d["code"]: d["count"] for d in summary["signal_distribution"]}
        assert counts["LATE_PAYMENTS"] == 1
        assert counts["CONTRACT_BLOCKED"] == 1

    def test_empty_summary(self, organization_a: Organization) -> None:
        summary = compute_churn_risk_summary(organization_a)
        assert summary["total_at_risk"] == 0
        assert summary["revenue_at_risk"] == 0.0
        assert summary["computed_at"] is None

    def test_top_customers_ordered(self, churn_scenario: Organization) -> None:
        compute_churn_risk_scores(churn_scenario)
        top = compute_top_risk_customers(churn_scenario, limit=20)
        assert [r["name"] for r in top] == ["Cliente Alto Risco", "Cliente Bloqueado"]
        assert top[0]["score"] == 60
        assert top[0]["level"] == "HIGH"
        assert len(top[0]["signals"]) == 3


# =============================================================================
# Views — /risk/ + alerta no executivo
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestRiskViews:
    def test_requires_login(self, client: Any) -> None:
        resp = client.get("/risk/")
        assert resp.status_code == 302

    def test_renders_with_at_risk_customer(
        self, client: Any, user_a: User, churn_scenario: Organization
    ) -> None:
        compute_churn_risk_scores(churn_scenario)
        client.force_login(user_a)
        resp = client.get("/risk/")
        assert resp.status_code == 200
        assert b"Cliente Alto Risco" in resp.content

    # Ignora warning pré-existente (não relacionado a esta feature): aggregations
    # do executivo comparam DateTimeField com data naive ao montar séries mensais.
    @pytest.mark.filterwarnings("ignore:DateTimeField .* received a naive datetime")
    def test_executive_shows_risk_alert(
        self, client: Any, user_a: User, churn_scenario: Organization
    ) -> None:
        compute_churn_risk_scores(churn_scenario)
        client.force_login(user_a)
        resp = client.get("/executive/")
        assert resp.status_code == 200
        assert b"alto risco de churn" in resp.content
