"""Testes dos lookups de OS (assunto/técnico) — issue #16.

Cobre o comando sync_os_lookups (popula o OsLookupCache a partir de
su_oss_assunto + funcionarios), o erro sem credenciais, e o resolver
load_os_lookups (resolução + fallback gracioso quando não sincronizado).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.helpdesk.application.os_lookups import load_os_lookups
from apps.helpdesk.infrastructure.models import OsLookupCache
from apps.integrations.ixc.client import IxcHttpClient
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization, OrganizationDataSource


def _seed_ixc_credentials(org: Organization) -> None:
    set_current_organization(org)
    ds = OrganizationDataSource.objects.create(
        organization=org,
        source_type=SourceType.IXC.value,
        capability=Capability.CUSTOMERS.value,
        priority=100,
        is_active=True,
    )
    ds.set_credentials(
        {"base_url": "https://erp.example.com", "user_id": "1", "api_token": "tok-xyz"}
    )
    ds.save()


def _fake_paginate(endpoints: dict[str, list[dict[str, Any]]]):
    def _paginate(self: Any, endpoint: str, **_kw: Any) -> Iterator[dict[str, Any]]:
        return iter(endpoints.get(endpoint, []))

    return _paginate


@pytest.mark.django_db
def test_sync_populates_cache(
    organization_a: Organization, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ixc_credentials(organization_a)
    monkeypatch.setattr(
        IxcHttpClient,
        "paginate_ixc",
        _fake_paginate({
            "su_oss_assunto": [
                {"id": "164", "assunto": "Inclusão SPC"},
                {"id": "10", "assunto": "Instalação"},
                {"id": "", "assunto": "ignorado"},          # sem id → ignora
                {"id": "99", "assunto": ""},                  # sem nome → ignora
            ],
            "funcionarios": [
                {"id": "49", "funcionario": "Pablo Técnico", "ativo": "S"},
                {"id": "5", "funcionario": "", "ativo": "S"},  # sem nome → ignora
            ],
        }),
    )

    call_command("sync_os_lookups", "acme")

    cache = OsLookupCache.objects.get(organization=organization_a)
    assert cache.subject_map == {"164": "Inclusão SPC", "10": "Instalação"}
    assert cache.technician_map == {"49": "Pablo Técnico"}
    assert cache.synced_at is not None


@pytest.mark.django_db
def test_sync_is_idempotent(
    organization_a: Organization, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_ixc_credentials(organization_a)
    monkeypatch.setattr(
        IxcHttpClient,
        "paginate_ixc",
        _fake_paginate({
            "su_oss_assunto": [{"id": "10", "assunto": "Instalação"}],
            "funcionarios": [{"id": "49", "funcionario": "Pablo Técnico"}],
        }),
    )

    call_command("sync_os_lookups", "acme")
    call_command("sync_os_lookups", "acme")

    assert OsLookupCache.objects.filter(organization=organization_a).count() == 1


@pytest.mark.django_db
def test_sync_without_credentials_fails(organization_a: Organization) -> None:
    with pytest.raises(CommandError, match="sem credenciais IXC"):
        call_command("sync_os_lookups", "acme")


@pytest.mark.django_db
def test_resolver_resolves_and_falls_back(organization_a: Organization) -> None:
    OsLookupCache.objects.create(
        organization=organization_a,
        subject_map={"10": "Instalação"},
        technician_map={"49": "Pablo Técnico"},
    )

    lookups = load_os_lookups(organization_a)
    # Resolve conhecidos
    assert lookups.subject_name("10") == "Instalação"
    assert lookups.technician_name("49") == "Pablo Técnico"
    # Fallback legível pra IDs desconhecidos
    assert lookups.subject_name("999") == "Assunto #999"
    assert lookups.technician_name("999") == "Técnico #999"
    # Vazio → rótulo neutro
    assert lookups.subject_name("") == "(Sem assunto)"
    assert lookups.technician_name(None) == "(Sem técnico)"


@pytest.mark.django_db
def test_resolver_graceful_when_not_synced(organization_a: Organization) -> None:
    # Org sem OsLookupCache — resolver não quebra, cai no fallback.
    lookups = load_os_lookups(organization_a)
    assert lookups.subject_name("10") == "Assunto #10"
    assert lookups.technician_name("49") == "Técnico #49"
