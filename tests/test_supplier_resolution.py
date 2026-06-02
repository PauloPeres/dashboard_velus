"""Testes da resolução de nomes de fornecedores no DRE-Contas (#23).

O bug original deixava `Fornecedor #519` em `Expense.supplier_name` quando o
nome não resolvia no momento do sync. O FornecedorCache persiste o mapa
id→nome e o DRE-Contas re-resolve em tempo de exibição.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics.application.aggregations import (
    _resolve_supplier_name,
    compute_dre_by_account,
)
from apps.analytics.infrastructure.models import FornecedorCache
from apps.financial.infrastructure.models import Expense
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_seq = 0


def _make_expense(
    org: Organization,
    *,
    supplier_external_id: str,
    supplier_name: str,
    amount: Decimal,
    id_conta: str = "100",
) -> Expense:
    global _seq
    _seq += 1
    set_current_organization(org)
    paid = timezone.now().date() - timedelta(days=5)
    return Expense.objects.create(
        organization=org,
        source_type="FAKE",
        external_id=f"exp-{_seq}",
        supplier_external_id=supplier_external_id,
        supplier_name=supplier_name,
        amount=amount,
        due_date=paid,
        paid_at=paid,
        status="PAID",
        raw_extras={"id_conta": id_conta},
    )


class TestResolveSupplierName:
    def test_cache_overrides_anonymous_fallback(self) -> None:
        name = _resolve_supplier_name("519", "Fornecedor #519", {"519": "Acme Links"})
        assert name == "Acme Links"

    def test_cache_preferred_over_stored(self) -> None:
        name = _resolve_supplier_name("519", "Nome Antigo", {"519": "Nome Novo"})
        assert name == "Nome Novo"

    def test_falls_back_to_stored_when_not_cached(self) -> None:
        name = _resolve_supplier_name("777", "Fornecedor Local", {})
        assert name == "Fornecedor Local"

    def test_ignores_cached_anonymous_name(self) -> None:
        # cache também anônimo → mantém o stored
        name = _resolve_supplier_name("519", "Fornecedor #519", {"519": "Fornecedor #519"})
        assert name == "Fornecedor #519"

    def test_no_id_keeps_stored(self) -> None:
        name = _resolve_supplier_name("0", "Pago à vista", {"0": "x"})
        assert name == "Pago à vista"

    def test_empty_everything_returns_placeholder(self) -> None:
        assert _resolve_supplier_name("", "", {}) == "(Sem fornecedor)"


@pytest.mark.django_db
class TestDreSupplierResolution:
    def test_dre_resolves_anonymous_supplier(self, organization_a: Organization) -> None:
        _make_expense(
            organization_a,
            supplier_external_id="519",
            supplier_name="Fornecedor #519",
            amount=Decimal("1000"),
        )
        FornecedorCache.objects.create(
            organization=organization_a,
            supplier_map={"519": "Acme Conectividade"},
            synced_at=timezone.now(),
        )

        set_current_organization(organization_a)
        result = compute_dre_by_account(organization_a, months=3)

        all_suppliers = [
            s["name"]
            for cat in result["categories"]
            for s in cat["suppliers"]
        ]
        assert "Acme Conectividade" in all_suppliers
        assert not any(n.startswith("Fornecedor #") for n in all_suppliers)

    def test_dre_keeps_stored_name_without_cache(
        self, organization_a: Organization
    ) -> None:
        _make_expense(
            organization_a,
            supplier_external_id="888",
            supplier_name="Energia SA",
            amount=Decimal("500"),
        )
        set_current_organization(organization_a)
        result = compute_dre_by_account(organization_a, months=3)
        all_suppliers = [
            s["name"] for cat in result["categories"] for s in cat["suppliers"]
        ]
        assert "Energia SA" in all_suppliers
