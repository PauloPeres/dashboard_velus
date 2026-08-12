"""Reconciliação noturna de faturas — issue #132.

Faturas eram a única fonte de dinheiro sem rede de segurança: despesas e
pagamentos já tinham reconciliação noturna, faturas não. Foi exatamente aí que
seis semanas de caixa se perderam quando o incremental parou de trazer registro.

A diferença desta reconciliação pras outras é a JANELA: são ~111 mil faturas
contra ~10 mil despesas, e o re-upsert regrava tudo. Puxar só o que foi
atualizado na janela dá o mesmo conserto por uma fração do custo — mas cobra um
preço: com janela não dá pra apagar por ausência, porque o que ficou de fora
pode simplesmente não ter sido atualizado.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.financial.domain.dto import InvoiceDTO
from apps.financial.infrastructure.models import Invoice
from apps.integrations.fake.invoices import FakeInvoiceSource
from apps.integrations.shared.enums import Capability
from apps.shared.context import set_current_organization
from apps.sync.tasks import (
    _RECONCILABLE,
    _RECONCILE_REUPSERT,
    _RECONCILE_WINDOW,
    reconcile_capability,
    sync_capability,
)
from apps.tenancy.models import Organization, OrganizationDataSource


def _fatura(ext: str, *, emitida_dias_atras: int, status: str = "PENDING") -> InvoiceDTO:
    agora = timezone.now()
    return InvoiceDTO(
        external_id=ext,
        contract_external_id="ctr-1",
        amount=Decimal("100.00"),
        due_date=(agora - timedelta(days=emitida_dias_atras)).date(),
        issued_at=agora - timedelta(days=emitida_dias_atras),
        paid_at=None if status != "PAID" else agora,
        status=status,
    )


class TestConfiguracao:
    def test_faturas_entraram_na_reconciliacao(self) -> None:
        """O buraco que causou o incidente."""
        assert Capability.INVOICES in _RECONCILABLE
        assert Capability.INVOICES in _RECONCILE_REUPSERT

    def test_faturas_reconciliam_por_janela(self) -> None:
        assert _RECONCILE_WINDOW[Capability.INVOICES] == timedelta(days=180)

    def test_despesas_seguem_com_pull_completo(self) -> None:
        """Volume baixo: não precisa de janela, e assim mantém o soft-delete."""
        assert Capability.EXPENSES not in _RECONCILE_WINDOW

    def test_janela_de_faturas_e_obrigatoria(self) -> None:
        """Não é só custo: `InvoiceRepository` não implementa soft-delete.

        Sem janela, a reconciliação cairia no ramo de apagar-por-ausência e
        quebraria. Tirar INVOICES da janela exige implementar isso antes.
        """
        from apps.financial.infrastructure.repositories import InvoiceRepository

        assert not hasattr(InvoiceRepository, "soft_delete_missing")


@pytest.mark.django_db
class TestReconciliacaoDeFaturas:
    def test_pagamento_perdido_pelo_incremental_e_recuperado(
        self,
        organization_a: Organization,
        datasource_fake_invoices_a: OrganizationDataSource,
    ) -> None:
        """O cenário do incidente, em miniatura.

        A fatura entra em aberto. Depois é paga — e o pagamento não volta pelo
        incremental. A reconciliação noturna é quem conserta.
        """
        FakeInvoiceSource.set_seed([_fatura("inv-1", emitida_dias_atras=30)])
        sync_capability(
            organization_id=organization_a.pk,
            capability="INVOICES",
            mode="BOOTSTRAP",
        )
        set_current_organization(organization_a)
        assert Invoice.objects.get(external_id="inv-1").status == "PENDING"

        # Agora ela está paga no sistema de origem.
        FakeInvoiceSource.set_seed(
            [_fatura("inv-1", emitida_dias_atras=30, status="PAID")]
        )
        reconcile_capability(
            organization_id=organization_a.pk, capability="INVOICES"
        )

        set_current_organization(organization_a)
        assert Invoice.objects.get(external_id="inv-1").status == "PAID"

    def test_janela_nao_apaga_o_que_ficou_de_fora(
        self,
        organization_a: Organization,
        datasource_fake_invoices_a: OrganizationDataSource,
    ) -> None:
        """A armadilha da janela — e por que o soft-delete some junto com ela.

        Uma fatura antiga não aparece no pull da janela. Se a reconciliação
        apagasse por ausência, ela sumiria do banco sem nunca ter sido apagada
        na origem — e `InvoiceRepository` nem implementa soft-delete, então o
        ramo de apagar quebraria antes disso.
        """
        antiga = _fatura("inv-antiga", emitida_dias_atras=400)
        nova = _fatura("inv-nova", emitida_dias_atras=10)
        FakeInvoiceSource.set_seed([antiga, nova])
        sync_capability(
            organization_id=organization_a.pk,
            capability="INVOICES",
            mode="BOOTSTRAP",
        )

        resultado = reconcile_capability(
            organization_id=organization_a.pk, capability="INVOICES"
        )

        set_current_organization(organization_a)
        assert Invoice.objects.filter(external_id="inv-antiga").exists()
        assert resultado["sources"][0]["soft_deleted"] == 0
        assert resultado["sources"][0]["janela_dias"] == 180

    def test_so_a_janela_e_percorrida(
        self,
        organization_a: Organization,
        datasource_fake_invoices_a: OrganizationDataSource,
    ) -> None:
        """O ponto do custo: 111 mil faturas não são regravadas toda noite."""
        FakeInvoiceSource.set_seed(
            [
                _fatura("inv-antiga", emitida_dias_atras=400),
                _fatura("inv-nova", emitida_dias_atras=10),
            ]
        )
        resultado = reconcile_capability(
            organization_id=organization_a.pk, capability="INVOICES"
        )
        assert resultado["records_processed"] == 1  # só a de 10 dias


@pytest.mark.django_db
class TestOutrasCapabilities:
    def test_pagamentos_seguem_com_soft_delete(
        self,
        organization_a: Organization,
        datasource_fake_payments_a: OrganizationDataSource,
    ) -> None:
        """A mudança não pode ter mexido em quem já funcionava."""
        from apps.integrations.fake.invoices import FakePaymentSource

        FakePaymentSource.set_seed([])
        resultado = reconcile_capability(
            organization_id=organization_a.pk, capability="PAYMENTS"
        )
        assert "janela_dias" not in resultado["sources"][0]
        assert "soft_deleted" in resultado["sources"][0]
