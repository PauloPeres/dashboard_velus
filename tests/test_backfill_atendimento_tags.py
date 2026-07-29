"""Testes do comando `backfill_atendimento_tags`.

Cobre o helper `extract_tag_ids` (parse do raw_extras) e o backfill em si:
resolve id_tag -> nome via catalogo Etiqueta, idempotencia e dry-run.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from apps.atendimento.infrastructure.models import Atendimento, Etiqueta
from apps.atendimento.management.commands.backfill_atendimento_tags import (
    extract_tag_ids,
)
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization


class TestExtractTagIds:
    def test_extracts_dedup_ordered(self) -> None:
        raw = {
            "tags": [
                {"_id": "app1", "id_tag": "t-sup", "data": "2026-01-01"},
                {"_id": "app2", "id_tag": "t-com"},
                {"_id": "app3", "id_tag": "t-sup"},  # duplicada
            ]
        }
        assert extract_tag_ids(raw) == ["t-sup", "t-com"]

    def test_empty_when_no_tags(self) -> None:
        assert extract_tag_ids({"origem": "bot"}) == []
        assert extract_tag_ids({"tags": []}) == []
        assert extract_tag_ids(None) == []
        assert extract_tag_ids({"tags": "nope"}) == []


def _make_atendimento(
    org: Organization, *, external_id: str, tags_raw: list[dict], tags: list | None = None
) -> Atendimento:
    return Atendimento.objects.create(
        organization=org,
        source_type=SourceType.OPA.value,
        external_id=external_id,
        status=Atendimento.Status.CLOSED.value,
        tags=tags or [],
        raw_extras={"tags": tags_raw},
    )


@pytest.mark.django_db
class TestBackfillCommand:
    def _catalog(self, org: Organization) -> None:
        for ext, nome in [("t-sup", "Suporte"), ("t-com", "Comercial")]:
            Etiqueta.objects.create(
                organization=org,
                source_type=SourceType.OPA.value,
                external_id=ext,
                nome=nome,
            )

    def test_backfill_resolves_names_from_raw_extras(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        self._catalog(organization_a)
        _make_atendimento(
            organization_a,
            external_id="a1",
            tags_raw=[{"id_tag": "t-sup"}, {"id_tag": "t-com"}],
        )
        # id sem catalogo -> fallback rastreavel (mantem o id)
        _make_atendimento(
            organization_a,
            external_id="a2",
            tags_raw=[{"id_tag": "t-sup"}, {"id_tag": "t-removida"}],
        )

        out = StringIO()
        call_command(
            "backfill_atendimento_tags",
            "acme",
            "--skip-catalog-sync",
            stdout=out,
        )

        set_current_organization(organization_a)
        assert Atendimento.objects.get(external_id="a1").tags == ["Suporte", "Comercial"]
        assert Atendimento.objects.get(external_id="a2").tags == ["Suporte", "t-removida"]
        assert "2 atualizados" in out.getvalue()

    def test_dry_run_does_not_write(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        self._catalog(organization_a)
        _make_atendimento(organization_a, external_id="a1", tags_raw=[{"id_tag": "t-sup"}])

        call_command(
            "backfill_atendimento_tags",
            "acme",
            "--skip-catalog-sync",
            "--dry-run",
            stdout=StringIO(),
        )

        set_current_organization(organization_a)
        assert Atendimento.objects.get(external_id="a1").tags == []

    def test_idempotent_second_run_updates_nothing(self, organization_a: Organization) -> None:
        set_current_organization(organization_a)
        self._catalog(organization_a)
        _make_atendimento(organization_a, external_id="a1", tags_raw=[{"id_tag": "t-sup"}])
        call_command(
            "backfill_atendimento_tags",
            "acme",
            "--skip-catalog-sync",
            stdout=StringIO(),
        )
        out = StringIO()
        call_command(
            "backfill_atendimento_tags",
            "acme",
            "--skip-catalog-sync",
            stdout=out,
        )
        assert "0 atualizados" in out.getvalue()
