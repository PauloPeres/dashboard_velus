"""Testes do comando setup_ixc_credentials.

Garante que o comando configura OrganizationDataSource pra TODAS as capabilities
que o adapter IXC registra no SourceRegistry — e não só um subconjunto hardcoded.
Esse é o contrato que faz o beat (dispatch_incremental_for_all_orgs) varrer/
sincronizar tudo: capability sem datasource nunca é escaneada.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.registry import registry
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource


def _ixc_capabilities() -> set[str]:
    return {
        cap.value
        for cap in Capability
        if registry.get_factory(SourceType.IXC, cap) is not None
    }


@pytest.mark.django_db
def test_configures_all_registered_ixc_capabilities(
    organization_a: Organization, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IXC_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("IXC_USER_ID", "1")
    monkeypatch.setenv("IXC_API_TOKEN", "secret-token-123")

    call_command("setup_ixc_credentials", "acme", "--non-interactive")

    set_current_organization(organization_a)
    configured = set(
        OrganizationDataSource.objects.filter(
            organization=organization_a,
            source_type=SourceType.IXC.value,
            is_active=True,
        ).values_list("capability", flat=True)
    )
    expected = _ixc_capabilities()
    # O adapter IXC registra as 11 capabilities — todas devem virar datasource.
    assert expected == configured
    assert Capability.TICKETS.value in configured
    assert Capability.PAYMENTS.value in configured
    assert Capability.CONNECTIONS.value in configured


@pytest.mark.django_db
def test_rerun_is_idempotent(
    organization_a: Organization, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IXC_BASE_URL", "https://erp.example.com")
    monkeypatch.setenv("IXC_USER_ID", "1")
    monkeypatch.setenv("IXC_API_TOKEN", "secret-token-123")

    call_command("setup_ixc_credentials", "acme", "--non-interactive")
    call_command("setup_ixc_credentials", "acme", "--non-interactive")

    set_current_organization(organization_a)
    count = OrganizationDataSource.objects.filter(
        organization=organization_a, source_type=SourceType.IXC.value
    ).count()
    assert count == len(_ixc_capabilities())
