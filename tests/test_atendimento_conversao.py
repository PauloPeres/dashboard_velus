"""Testes de `compute_atendimento_conversao` (F4 — churn/conversão por tag/motivo).

Cobre: churn = cancelamento após a conversa dentro do horizonte; conversão =
ativação após a conversa; atribuição por tag/motivo; só vinculados; smoke da view.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import compute_atendimento_conversao
from apps.atendimento.infrastructure.models import Atendimento
from apps.customers.infrastructure.models import Contract, Customer
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, User


def _customer(org: Organization, *, doc: str) -> Customer:
    return Customer.objects.create(
        organization=org, source_type=SourceType.IXC.value,
        external_id=f"c-{doc}", document=doc, name=f"Cli {doc}",
        status=Customer.Status.ACTIVE.value,
    )


def _contract(
    org: Organization, cust: Customer, *, ext: str, status: str,
    canceled_at: Any = None, activated_at: Any = None, mrr: str = "100.00",
) -> Contract:
    return Contract.objects.create(
        organization=org, source_type=SourceType.IXC.value, external_id=ext,
        customer=cust, customer_external_id=cust.external_id,
        plan_name="P", monthly_amount=Decimal(mrr), status=status,
        canceled_at=canceled_at, activated_at=activated_at,
    )


def _at(
    org: Organization, *, ext: str, cust: Customer | None, opened_at: Any,
    tags: list[str] | None = None, motivos: list[str] | None = None,
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org, source_type=SourceType.OPA.value, external_id=ext,
        customer=cust, customer_document=cust.document if cust else "",
        status=Atendimento.Status.CLOSED.value, opened_at=opened_at,
        tags=tags or [], motivos=motivos or [],
    )


def _by_name(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


@pytest.mark.django_db
class TestAtendimentoConversao:
    def test_churn_after_conversation_counted(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        cust = _customer(organization_a, doc="111")
        # Cancelou 10 dias DEPOIS da conversa -> dentro do horizonte de 90d.
        _contract(
            organization_a, cust, ext="k1", status="CANCELED",
            canceled_at=now - timedelta(days=20), mrr="150.00",
        )
        _at(organization_a, ext="a1", cust=cust,
            opened_at=now - timedelta(days=30), tags=["Bloqueio"])

        data = compute_atendimento_conversao(organization_a, months=6, horizon_days=90)
        assert data["total_linked"] == 1
        assert data["churn_total"] == 1
        row = _by_name(data["by_tag"], "Bloqueio")
        assert row["churn"] == 1
        assert row["churn_pct"] == 100.0
        assert row["mrr_churn"] == 150.0

    def test_cancel_before_conversation_not_counted(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        cust = _customer(organization_a, doc="222")
        # Cancelou ANTES da conversa -> não conta como churn "após".
        _contract(
            organization_a, cust, ext="k1", status="CANCELED",
            canceled_at=now - timedelta(days=40),
        )
        _at(organization_a, ext="a1", cust=cust,
            opened_at=now - timedelta(days=30), tags=["Suporte"])
        data = compute_atendimento_conversao(organization_a, months=6, horizon_days=90)
        assert data["churn_total"] == 0

    def test_cancel_outside_horizon_not_counted(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        cust = _customer(organization_a, doc="333")
        # Conversa há 200d, cancelou há 100d -> 100d depois, fora do horizonte 90d.
        _contract(
            organization_a, cust, ext="k1", status="CANCELED",
            canceled_at=now - timedelta(days=100),
        )
        _at(organization_a, ext="a1", cust=cust,
            opened_at=now - timedelta(days=200), tags=["X"])
        data = compute_atendimento_conversao(organization_a, months=12, horizon_days=90)
        assert data["churn_total"] == 0

    def test_conversion_after_conversation(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        cust = _customer(organization_a, doc="444")
        _contract(
            organization_a, cust, ext="k1", status="ACTIVE",
            activated_at=now - timedelta(days=15),
        )
        _at(organization_a, ext="a1", cust=cust,
            opened_at=now - timedelta(days=25), motivos=["comercial"])
        data = compute_atendimento_conversao(organization_a, months=6, horizon_days=90)
        assert data["conv_total"] == 1
        assert _by_name(data["by_motivo"], "comercial")["conv_pct"] == 100.0

    def test_unlinked_atendimento_ignored(
        self, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        _at(organization_a, ext="a1", cust=None,
            opened_at=now - timedelta(days=5), tags=["Y"])
        data = compute_atendimento_conversao(organization_a, months=6)
        assert data["total_linked"] == 0
        assert data["by_tag"] == []


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestConversaoView:
    URL = "/operations/atendimento-conversao/"

    def test_requires_login(self, client: Any) -> None:
        assert client.get(self.URL).status_code == 302

    def test_renders(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        set_current_organization(organization_a)
        now = timezone.now()
        cust = _customer(organization_a, doc="999")
        _contract(organization_a, cust, ext="k1", status="CANCELED",
                  canceled_at=now - timedelta(days=10))
        _at(organization_a, ext="a1", cust=cust,
            opened_at=now - timedelta(days=20), tags=["Bloqueio"])
        client.force_login(user_a)
        resp = client.get(f"{self.URL}?h=90")
        assert resp.status_code == 200
        assert b"churn-tags-chart" in resp.content
        assert b"Conversa" in resp.content
