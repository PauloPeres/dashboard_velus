"""Testes do comando bootstrap_all — carga total de todas as capabilities da org.

Cobre o modo inline (--sync, roda e persiste), o modo Celery (dispatch pra fila)
e o erro quando a org não tem datasource ativa.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.customers.infrastructure.models import Customer
from apps.integrations.fake.contracts import FakeContractSource
from apps.integrations.fake.customers import FakeCustomerSource
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource


@pytest.mark.django_db
@pytest.mark.e2e
def test_sync_mode_runs_inline_and_persists(
    organization_a: Organization,
    datasource_fake_customers_a: OrganizationDataSource,
    datasource_fake_contracts_a: OrganizationDataSource,
) -> None:
    FakeCustomerSource.set_seed([
        CustomerDTO(external_id="ext-1", document="11111111111", name="Cliente 1",
                    status="ACTIVE", created_at_source=datetime(2025, 1, 1, tzinfo=UTC)),
    ])
    FakeContractSource.set_seed([
        ContractDTO(external_id="ctr-1", customer_external_id="ext-1", plan_name="P",
                    monthly_amount=Decimal("100.00"), status="ACTIVE",
                    activated_at=datetime(2025, 1, 2, tzinfo=UTC)),
    ])

    call_command("bootstrap_all", "acme", "--sync")

    set_current_organization(organization_a)
    assert Customer.objects.filter(external_id="ext-1").exists()


@pytest.mark.django_db
def test_celery_mode_dispatches_per_capability(
    organization_a: Organization,
    datasource_fake_customers_a: OrganizationDataSource,
    datasource_fake_contracts_a: OrganizationDataSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apps.sync.tasks import sync_capability

    dispatched: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sync_capability, "apply_async",
        lambda **kw: dispatched.append(kw),
    )

    call_command("bootstrap_all", "acme")

    caps = {d["kwargs"]["capability"] for d in dispatched}
    assert caps == {"CUSTOMERS", "CONTRACTS"}
    assert all(d["kwargs"]["mode"] == "BOOTSTRAP" for d in dispatched)
    assert all(d["queue"] == "tenant_acme" for d in dispatched)


@pytest.mark.django_db
def test_errors_without_active_datasource(organization_a: Organization) -> None:
    with pytest.raises(CommandError, match="nenhuma datasource ativa"):
        call_command("bootstrap_all", "acme")
