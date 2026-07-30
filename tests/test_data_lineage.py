"""Testes da linhagem de fonte de dados por página (#67)."""

from __future__ import annotations

from typing import Any

import pytest

from apps.dashboards.data_lineage import lineage_for
from apps.tenancy.models import User


class TestLineageFor:
    def test_opa_pages(self) -> None:
        srcs = lineage_for("dashboards", "atendimento_tendencias")
        labels = [s["source"] for s in srcs]
        assert "Opa Suite" in labels
        assert "IXC" in labels  # marcador de vencimento

    def test_financial_is_ixc(self) -> None:
        assert lineage_for("dashboards", "financial")[0]["source"] == "IXC"

    def test_detail_alias_resolves(self) -> None:
        # rota de detalhe herda a página pai (customer_detail -> customers)
        assert lineage_for("dashboards", "customer_detail")[0]["source"] == "IXC"

    def test_non_page_empty(self) -> None:
        assert lineage_for("admin", "index") == []


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestLineageBadgeRendered:
    def test_badge_on_opa_page(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        html = client.get("/operations/atendimento-tendencias/").content
        assert b"Opa Suite" in html
        assert b"/atendimento" in html  # detalhe das APIs no tooltip

    def test_badge_on_financial_page(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        html = client.get("/financial/").content
        assert b"Fonte:" in html
        assert b"IXC" in html
