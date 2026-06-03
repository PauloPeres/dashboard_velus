"""Testes das melhorias de churn — novos sinais, ML, digests e opt-in.

Cobre:
  - sinal de downgrade de plano (SCD2 DimContract)
  - sinal de queda brusca de banda (BandwidthUsage)
  - recompute de churn no signal sync_completed
  - treino + scoring ML (regressão logística pura-Python) + fallback
  - digests por email com opt-in por usuário
  - view de preferências /settings/
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.core import mail
from django.utils import timezone

from apps.analytics.application.churn_digest import (
    build_digest,
    send_churn_digest,
)
from apps.analytics.application.churn_ml import (
    FEATURES,
    _os_signals,
    _payment_profile,
    compute_features,
    get_current_model,
    predict_probabilities,
    train_churn_model,
)
from apps.analytics.application.churn_risk import compute_churn_risk_scores
from apps.analytics.infrastructure.models import ChurnRiskScore
from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.financial.domain.dto import InvoiceDTO
from apps.integrations.fake.bandwidth import FakeBandwidthUsageSource
from apps.integrations.fake.connections import FakeConnectionSource
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.integrations.fake.invoices import FakeInvoiceSource
from apps.network.domain.dto import BandwidthUsageDTO, ConnectionDTO
from apps.shared.context import set_current_organization
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource, User


def _sync(org: Organization, capability: str) -> None:
    sync_capability(organization_id=org.pk, capability=capability, mode="BOOTSTRAP")


def _scores_by_ext(org: Organization) -> dict[str, ChurnRiskScore]:
    set_current_organization(org)
    return {
        s.customer.external_id: s
        for s in ChurnRiskScore.objects.select_related("customer").all()
    }


# =============================================================================
# Sinal de downgrade de plano
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestDowngradeSignal:
    def test_downgrade_fires_after_plan_reduction(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
    ) -> None:
        FakeCustomerSource.set_seed([
            CustomerDTO(external_id="ext-dg", document="44444444444",
                        name="Cliente Downgrade", status="ACTIVE",
                        created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        ])
        _sync(organization_a, "CUSTOMERS")

        # Versão inicial: plano de R$ 200.
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-dg", customer_external_id="ext-dg",
                        plan_name="Fibra 1G", monthly_amount=Decimal("200.00"),
                        status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ])
        _sync(organization_a, "CONTRACTS")

        # Downgrade: mesmo contrato cai para R$ 100 → nova versão SCD2.
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-dg", customer_external_id="ext-dg",
                        plan_name="Fibra 200M", monthly_amount=Decimal("100.00"),
                        status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ])
        _sync(organization_a, "CONTRACTS")

        compute_churn_risk_scores(organization_a)
        score = _scores_by_ext(organization_a)["ext-dg"]
        codes = {s["code"] for s in score.signals}
        assert "PLAN_DOWNGRADE" in codes

    def test_no_downgrade_when_plan_stable(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
    ) -> None:
        FakeCustomerSource.set_seed([
            CustomerDTO(external_id="ext-st", document="55555555555",
                        name="Cliente Estável", status="ACTIVE",
                        created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        ])
        _sync(organization_a, "CUSTOMERS")
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-st", customer_external_id="ext-st",
                        plan_name="Fibra 1G", monthly_amount=Decimal("200.00"),
                        status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ])
        _sync(organization_a, "CONTRACTS")
        compute_churn_risk_scores(organization_a)
        assert "ext-st" not in _scores_by_ext(organization_a)


# =============================================================================
# Sinal de queda de banda
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestBandwidthDropSignal:
    def test_bandwidth_drop_fires(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
        datasource_fake_bandwidth_a: OrganizationDataSource,
    ) -> None:
        today = timezone.now().date()
        FakeCustomerSource.set_seed([
            CustomerDTO(external_id="ext-bw", document="66666666666",
                        name="Cliente Banda", status="ACTIVE",
                        created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        ])
        _sync(organization_a, "CUSTOMERS")
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-bw", customer_external_id="ext-bw",
                        plan_name="Fibra 1G", monthly_amount=Decimal("200.00"),
                        status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ])
        _sync(organization_a, "CONTRACTS")

        # Janela anterior (≈45d atrás): 10 GB. Janela recente (≈10d): ~0.
        FakeBandwidthUsageSource.set_seed([
            BandwidthUsageDTO(external_id="bw-prior", customer_external_id="ext-bw",
                              download_bytes=10_000_000_000, upload_bytes=0,
                              reference_date=today - timedelta(days=45)),
            BandwidthUsageDTO(external_id="bw-recent", customer_external_id="ext-bw",
                              download_bytes=50_000_000, upload_bytes=0,
                              reference_date=today - timedelta(days=10)),
        ])
        _sync(organization_a, "BANDWIDTH")

        compute_churn_risk_scores(organization_a)
        score = _scores_by_ext(organization_a)["ext-bw"]
        codes = {s["code"] for s in score.signals}
        assert "BANDWIDTH_DROP" in codes


# =============================================================================
# Recompute via signal sync_completed
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestSyncRecompute:
    def test_scores_materialized_after_sync(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
    ) -> None:
        FakeCustomerSource.set_seed([
            CustomerDTO(external_id="ext-bl", document="77777777777",
                        name="Cliente Bloqueado", status="BLOCKED",
                        created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        ])
        _sync(organization_a, "CUSTOMERS")
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-bl", customer_external_id="ext-bl",
                        plan_name="Fibra 200M", monthly_amount=Decimal("100.00"),
                        status="BLOCKED", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ])
        # Sem chamar compute explicitamente — o signal sync_completed recomputa.
        _sync(organization_a, "CONTRACTS")
        set_current_organization(organization_a)
        assert ChurnRiskScore.objects.filter(customer__external_id="ext-bl").exists()


# =============================================================================
# ML — features, treino, predição e fallback
# =============================================================================
def _seed_ml_population(org: Organization) -> None:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    today = timezone.now().date()
    customers, contracts, invoices, connections = [], [], [], []

    # 20 churned (CANCELED, com atraso) → label positivo.
    for i in range(20):
        ext = f"ch-{i}"
        customers.append(CustomerDTO(
            external_id=ext, document=f"9{i:010d}", name=f"Churned {i}",
            status="CANCELED", created_at_source=base))
        contracts.append(ContractDTO(
            external_id=f"ct-{ext}", customer_external_id=ext, plan_name="P",
            monthly_amount=Decimal("100.00"), status="CANCELED",
            activated_at=base, canceled_at=datetime(2025, 6, 1, tzinfo=UTC)))
        for j in range(3):
            invoices.append(InvoiceDTO(
                external_id=f"iv-{ext}-{j}", contract_external_id=f"ct-{ext}",
                amount=Decimal("100.00"),
                due_date=today - timedelta(days=20 + j * 30), status="OVERDUE"))

    # 40 ativos saudáveis; os 10 primeiros offline (geram sinal de regra).
    for i in range(40):
        ext = f"ac-{i}"
        customers.append(CustomerDTO(
            external_id=ext, document=f"1{i:010d}", name=f"Active {i}",
            status="ACTIVE", created_at_source=base))
        contracts.append(ContractDTO(
            external_id=f"ct-{ext}", customer_external_id=ext, plan_name="P",
            monthly_amount=Decimal("150.00"), status="ACTIVE", activated_at=base))
        if i < 10:
            connections.append(ConnectionDTO(
                external_id=f"cn-{ext}", customer_external_id=ext,
                contract_external_id=f"ct-{ext}", login=ext, status="OFFLINE",
                ip="", nas_ip="10.0.0.1", rx_bytes=0, tx_bytes=0,
                last_connection_at=timezone.now() - timedelta(days=2)))

    FakeCustomerSource.set_seed(customers)
    _sync(org, "CUSTOMERS")
    FakeContractSource.set_seed(contracts)
    _sync(org, "CONTRACTS")
    FakeInvoiceSource.set_seed(invoices)
    _sync(org, "INVOICES")
    FakeConnectionSource.set_seed(connections)
    _sync(org, "CONNECTIONS")


@pytest.fixture
def ml_population(
    organization_a: Organization,
    datasource_fake_customers_a: OrganizationDataSource,
    datasource_fake_contracts_a: OrganizationDataSource,
    datasource_fake_invoices_a: OrganizationDataSource,
    datasource_fake_connections_a: OrganizationDataSource,
) -> Organization:
    _seed_ml_population(organization_a)
    return organization_a


@pytest.mark.django_db
@pytest.mark.e2e
class TestChurnML:
    def test_features_and_labels(self, ml_population: Organization) -> None:
        features, churned, active = compute_features(ml_population)
        assert len(features) == 60
        assert len(churned) == 20
        assert len(active) == 40
        # cada vetor tem todas as features esperadas
        any_vec = next(iter(features.values()))
        assert set(any_vec) == set(FEATURES)

    def test_train_persists_model(self, ml_population: Organization) -> None:
        summary = train_churn_model(ml_population)
        assert summary is not None
        assert summary["n_samples"] == 60
        assert summary["n_positive"] == 20
        model = get_current_model(ml_population)
        assert model is not None
        assert list(model.feature_names) == list(FEATURES)
        assert 0.0 <= model.train_accuracy <= 1.0

    def test_predict_in_range(self, ml_population: Organization) -> None:
        train_churn_model(ml_population)
        model = get_current_model(ml_population)
        features, _churned, _active = compute_features(ml_population)
        probs = predict_probabilities(model, features)
        assert probs
        assert all(0.0 <= p <= 1.0 for p in probs.values())

    def test_scoring_sets_ml_probability(self, ml_population: Organization) -> None:
        train_churn_model(ml_population)
        compute_churn_risk_scores(ml_population)
        set_current_organization(ml_population)
        # ativos com sinal (offline) viram linha e recebem ml_probability.
        rows = ChurnRiskScore.objects.filter(customer__external_id__startswith="ac-")
        assert rows.exists()
        assert any(r.ml_probability is not None for r in rows)

    def test_fallback_when_insufficient(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
    ) -> None:
        FakeCustomerSource.set_seed([
            CustomerDTO(external_id="ext-solo", document="88888888888",
                        name="Solo", status="ACTIVE",
                        created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
        ])
        _sync(organization_a, "CUSTOMERS")
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-solo", customer_external_id="ext-solo",
                        plan_name="P", monthly_amount=Decimal("100.00"),
                        status="ACTIVE", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
        ])
        _sync(organization_a, "CONTRACTS")
        assert train_churn_model(organization_a) is None
        assert get_current_model(organization_a) is None

    def test_train_records_validation_metrics(
        self, ml_population: Organization
    ) -> None:
        summary = train_churn_model(ml_population)
        assert summary is not None
        assert "val_auc" in summary
        assert "val_accuracy" in summary
        model = get_current_model(ml_population)
        # Holdout determinístico tem as duas classes → métricas computadas.
        assert model.val_auc is not None
        assert 0.0 <= model.val_auc <= 1.0
        assert 0.0 <= model.val_accuracy <= 1.0


# =============================================================================
# Features point-in-time — sem vazamento temporal (#15)
# =============================================================================
@pytest.mark.django_db
@pytest.mark.e2e
class TestChurnMLPointInTime:
    def test_late_payments_snapshot_at_cancellation(
        self,
        organization_a: Organization,
        datasource_fake_customers_a: OrganizationDataSource,
        datasource_fake_contracts_a: OrganizationDataSource,
        datasource_fake_invoices_a: OrganizationDataSource,
    ) -> None:
        from apps.customers.infrastructure.models import Customer

        canceled_at = datetime(2025, 1, 1, tzinfo=UTC)
        FakeCustomerSource.set_seed([
            CustomerDTO(external_id="ext-pit", document="33333333333",
                        name="Cliente PIT", status="CANCELED",
                        created_at_source=datetime(2024, 1, 1, tzinfo=UTC)),
        ])
        _sync(organization_a, "CUSTOMERS")
        FakeContractSource.set_seed([
            ContractDTO(external_id="ctr-pit", customer_external_id="ext-pit",
                        plan_name="P", monthly_amount=Decimal("100.00"),
                        status="CANCELED",
                        activated_at=datetime(2024, 1, 1, tzinfo=UTC),
                        canceled_at=canceled_at),
        ])
        _sync(organization_a, "CONTRACTS")
        # Fatura vencida ANTES do cancelamento (conta) e DEPOIS (não conta).
        FakeInvoiceSource.set_seed([
            InvoiceDTO(external_id="iv-before", contract_external_id="ctr-pit",
                       amount=Decimal("100.00"),
                       due_date=date(2024, 6, 1), status="OVERDUE"),
            InvoiceDTO(external_id="iv-after", contract_external_id="ctr-pit",
                       amount=Decimal("100.00"),
                       due_date=date(2025, 6, 1), status="OVERDUE"),
        ])
        _sync(organization_a, "INVOICES")

        features, churned, _active = compute_features(organization_a)
        set_current_organization(organization_a)
        cid = Customer.objects.get(external_id="ext-pit").id
        assert cid in churned
        vec = features[cid]
        # Só a fatura vencida antes do cancelamento entra no vetor.
        assert vec["late_payments"] == 1.0
        # Tenure fotografado no cancelamento (~366d), não até hoje.
        assert vec["tenure_days"] == 366.0


# =============================================================================
# Perfil de inadimplência relativo ao cliente (#19) — função pura
# =============================================================================
class TestPaymentProfile:
    """`_payment_profile` mede atraso *relativo ao próprio cliente*.

    `r` é a data/hora de referência (datetime); `r_date` é `r.date()`.
    A janela "recente" é de 90 dias antes de `r_date`.
    """

    R = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    R_DATE = R.date()

    def _paid(self, due: date, days_late: int) -> datetime:
        return datetime.combine(due + timedelta(days=days_late), datetime.min.time(), tzinfo=UTC)

    def test_no_invoices_returns_zero(self) -> None:
        baseline, recent_dev = _payment_profile([], self.R, self.R_DATE)
        assert baseline == 0.0
        assert recent_dev == 0.0

    def test_consistently_late_has_zero_deviation(self) -> None:
        # Cliente que SEMPRE atrasa 10 dias — atrasar faz parte do perfil dele.
        invoices = [
            (date(2026, d, 1), self._paid(date(2026, d, 1), 10)) for d in range(1, 6)
        ]
        baseline, recent_dev = _payment_profile(invoices, self.R, self.R_DATE)
        assert baseline == 10.0
        # Faturas recentes seguem o mesmo padrão → desvio ~0 (não é sinal).
        assert recent_dev == pytest.approx(0.0)

    def test_recent_escalation_positive_deviation(self) -> None:
        # Histórico pontual (0d), mas atrasando muito nos últimos 90 dias.
        old = [
            (date(2025, m, 1), self._paid(date(2025, m, 1), 0)) for m in range(1, 7)
        ]
        recent = [
            (date(2026, 4, 1), self._paid(date(2026, 4, 1), 30)),
            (date(2026, 5, 1), self._paid(date(2026, 5, 1), 30)),
        ]
        baseline, recent_dev = _payment_profile(old + recent, self.R, self.R_DATE)
        assert baseline == pytest.approx(0.0)  # mediana histórica pontual
        assert recent_dev > 25.0  # atraso recente muito acima do normal dele

    def test_future_due_ignored(self) -> None:
        # Fatura que vence DEPOIS da data de referência não conta.
        invoices = [
            (date(2026, 5, 1), self._paid(date(2026, 5, 1), 5)),
            (date(2026, 12, 1), None),  # futura
        ]
        baseline, _recent_dev = _payment_profile(invoices, self.R, self.R_DATE)
        assert baseline == 5.0

    def test_paid_after_reference_treated_as_open(self) -> None:
        # Vencida antes de `r`, paga só DEPOIS de `r` → tratada como em aberto.
        future_paid = datetime(2026, 7, 1, tzinfo=UTC)
        invoices = [(date(2026, 5, 1), future_paid)]
        baseline, recent_dev = _payment_profile(invoices, self.R, self.R_DATE)
        # Não liquidada em `r` → não entra no baseline (mediana de vazio = 0).
        assert baseline == 0.0
        # Atraso acumulado até `r_date`: (2026-06-01 - 2026-05-01) = 31 dias.
        assert recent_dev == pytest.approx(31.0)


# =============================================================================
# Sinais operacionais de OS (#20) — função pura
# =============================================================================
class TestOsSignals:
    """`_os_signals` mede recência e recorrência de OS, point-in-time até `r`."""

    R = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    def _os(self, days_before: int, subject: str = "10") -> tuple[datetime, str]:
        return (self.R - timedelta(days=days_before), subject)

    def test_no_tickets_returns_zero(self) -> None:
        recent, recurrence = _os_signals([], self.R)
        assert recent == 0.0
        assert recurrence == 0.0

    def test_recent_window_counts_last_90d(self) -> None:
        tickets = [self._os(10), self._os(80), self._os(120)]  # 120d fora da janela
        recent, _recurrence = _os_signals(tickets, self.R)
        assert recent == 2.0

    def test_future_tickets_ignored(self) -> None:
        # OS aberta DEPOIS da data de referência não conta (sem vazamento).
        tickets = [self._os(5), (self.R + timedelta(days=10), "10")]
        recent, _recurrence = _os_signals(tickets, self.R)
        assert recent == 1.0

    def test_recurrence_same_subject_within_window(self) -> None:
        # Duas OS do mesmo assunto a 5 dias → 1 recorrência.
        tickets = [self._os(20, "10"), self._os(15, "10")]
        _recent, recurrence = _os_signals(tickets, self.R)
        assert recurrence == 1.0

    def test_recurrence_different_subjects_not_counted(self) -> None:
        tickets = [self._os(20, "10"), self._os(15, "99")]
        _recent, recurrence = _os_signals(tickets, self.R)
        assert recurrence == 0.0

    def test_recurrence_outside_window_not_counted(self) -> None:
        # Mesmo assunto, mas 40 dias de intervalo (> 30d) → não é recorrência.
        tickets = [self._os(60, "10"), self._os(20, "10")]
        _recent, recurrence = _os_signals(tickets, self.R)
        assert recurrence == 0.0


# =============================================================================
# Digest por email + opt-in
# =============================================================================
@pytest.fixture
def churn_scenario_min(
    organization_a: Organization,
    datasource_fake_customers_a: OrganizationDataSource,
    datasource_fake_contracts_a: OrganizationDataSource,
) -> Organization:
    FakeCustomerSource.set_seed([
        CustomerDTO(external_id="ext-blk", document="12121212121",
                    name="Cliente Risco", status="BLOCKED",
                    created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
    ])
    _sync(organization_a, "CUSTOMERS")
    FakeContractSource.set_seed([
        ContractDTO(external_id="ctr-blk", customer_external_id="ext-blk",
                    plan_name="Fibra 200M", monthly_amount=Decimal("100.00"),
                    status="BLOCKED", activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
    ])
    _sync(organization_a, "CONTRACTS")
    compute_churn_risk_scores(organization_a)
    return organization_a


@pytest.mark.django_db
@pytest.mark.e2e
class TestChurnDigest:
    def test_build_digest_payload(self, churn_scenario_min: Organization) -> None:
        digest = build_digest(churn_scenario_min, "weekly")
        assert digest["period_label"] == "semanal"
        assert digest["summary"]["total_at_risk"] >= 1
        assert digest["top"]

    def test_weekly_carries_collections_not_strategic(
        self, churn_scenario_min: Organization
    ) -> None:
        digest = build_digest(churn_scenario_min, "weekly")
        assert digest["is_monthly"] is False
        assert "collections" in digest and "strategic" not in digest
        assert "over_90_str" in digest["collections"]

    def test_monthly_carries_strategic_block(
        self, churn_scenario_min: Organization
    ) -> None:
        digest = build_digest(churn_scenario_min, "monthly")
        assert digest["is_monthly"] is True
        assert "strategic" in digest and "collections" not in digest
        strat = digest["strategic"]
        for key in (
            "mrr_now_str", "churn_pct", "net_adds", "cash_month_str",
            "delinquency_str", "forecast_next_str",
        ):
            assert key in strat

    def test_send_only_to_optin_users(
        self, churn_scenario_min: Organization, user_a: User
    ) -> None:
        user_a.churn_digest_weekly = True
        user_a.save(update_fields=["churn_digest_weekly"])
        mail.outbox.clear()
        result = send_churn_digest(churn_scenario_min, "weekly")
        assert result["recipients"] == 1
        assert result["sent"] == 1
        assert len(mail.outbox) == 1
        assert "Foco da semana" in mail.outbox[0].subject

    def test_no_send_without_optin(
        self, churn_scenario_min: Organization, user_a: User
    ) -> None:
        mail.outbox.clear()
        result = send_churn_digest(churn_scenario_min, "weekly")
        assert result["recipients"] == 0
        assert len(mail.outbox) == 0

    def test_monthly_optin_separate_from_weekly(
        self, churn_scenario_min: Organization, user_a: User
    ) -> None:
        user_a.churn_digest_monthly = True
        user_a.save(update_fields=["churn_digest_monthly"])
        mail.outbox.clear()
        # opt-in mensal não dispara o semanal
        assert send_churn_digest(churn_scenario_min, "weekly")["recipients"] == 0
        assert send_churn_digest(churn_scenario_min, "monthly")["recipients"] == 1


# =============================================================================
# View de preferências
# =============================================================================
@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestSettingsView:
    def test_requires_login(self, client: Any) -> None:
        assert client.get("/settings/").status_code == 302

    def test_get_renders(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        resp = client.get("/settings/")
        assert resp.status_code == 200
        assert b"Digest semanal" in resp.content

    def test_post_saves_preferences(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        resp = client.post("/settings/", {"churn_digest_weekly": "1"})
        assert resp.status_code == 200
        user_a.refresh_from_db()
        assert user_a.churn_digest_weekly is True
        assert user_a.churn_digest_monthly is False

    def test_post_unchecking_disables(self, client: Any, user_a: User) -> None:
        user_a.churn_digest_weekly = True
        user_a.save(update_fields=["churn_digest_weekly"])
        client.force_login(user_a)
        resp = client.post("/settings/", {})
        assert resp.status_code == 200
        user_a.refresh_from_db()
        assert user_a.churn_digest_weekly is False
