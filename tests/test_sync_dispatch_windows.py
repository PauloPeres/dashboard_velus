"""Testes do escalonamento de capabilities no dispatch incremental (#31).

`dispatch_incremental_for_all_orgs` aceita um filtro `capabilities` pra que o
Beat possa disparar grupos de capabilities em janelas distintas, evitando
sobrecarregar a API do IXC com as 11 sincronizações ao mesmo tempo.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.integrations.shared.enums import Capability, SourceType
from apps.sync.tasks import _dispatch_incremental
from apps.tenancy.models import Organization, OrganizationDataSource


def _datasource(org: Organization, capability: Capability) -> None:
    ds = OrganizationDataSource.objects.create(
        organization=org,
        source_type=SourceType.FAKE.value,
        capability=capability.value,
        priority=100,
        is_active=True,
    )
    ds.set_credentials({})
    ds.save()


@pytest.mark.django_db
class TestDispatchIncrementalWindows:
    def _seed_all(self, org: Organization) -> None:
        for cap in (
            Capability.CUSTOMERS,
            Capability.CONTRACTS,
            Capability.INVOICES,
            Capability.TICKETS,
        ):
            _datasource(org, cap)

    def test_no_filter_dispatches_all(self, organization_a: Organization) -> None:
        self._seed_all(organization_a)
        with patch("apps.sync.tasks.sync_capability.apply_async") as mock:
            result = _dispatch_incremental()
        assert result["tasks_dispatched"] == 4
        assert mock.call_count == 4

    def test_filter_restricts_to_subset(self, organization_a: Organization) -> None:
        self._seed_all(organization_a)
        with patch("apps.sync.tasks.sync_capability.apply_async") as mock:
            result = _dispatch_incremental(["CUSTOMERS", "CONTRACTS"])
        assert result["tasks_dispatched"] == 2
        dispatched = {
            call.kwargs["kwargs"]["capability"] for call in mock.call_args_list
        }
        assert dispatched == {"CUSTOMERS", "CONTRACTS"}

    def test_filter_with_no_matching_datasource(
        self, organization_a: Organization
    ) -> None:
        _datasource(organization_a, Capability.CUSTOMERS)
        with patch("apps.sync.tasks.sync_capability.apply_async") as mock:
            result = _dispatch_incremental(["INVOICES"])
        assert result["tasks_dispatched"] == 0
        assert mock.call_count == 0
