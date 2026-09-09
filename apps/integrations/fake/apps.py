"""AppConfig do adapter Fake (in-memory).

Registra `FakeCustomerSource` no SourceRegistry quando o app é carregado.
Usado em testes e em ambiente de desenvolvimento sem credenciais reais.
"""

from __future__ import annotations

from django.apps import AppConfig


class FakeAdapterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations.fake"
    label = "integrations_fake"
    verbose_name = "Integrations: Fake (in-memory)"

    def ready(self) -> None:
        from apps.integrations.shared.enums import Capability, SourceType
        from apps.integrations.shared.registry import registry

        from .bandwidth import FakeBandwidthUsageSource
        from .connections import FakeConnectionSource
        from .contracts import FakeContractSource
        from .customers import FakeCustomerSource
        from .equipment import FakeEquipmentSource
        from .expenses import FakeExpenseSource
        from .invoices import FakeInvoiceSource, FakePaymentSource
        from .leads import FakeLeadSource
        from .network_elements import FakeNetworkElementSource
        from .opportunities import FakeOpportunitySource
        from .optical_signal import FakeOpticalSignalSource
        from .tickets import FakeTicketSource

        # Idempotência: AppConfig.ready() pode rodar 2x em dev reload.
        for cap, cls in [
            (Capability.CUSTOMERS, FakeCustomerSource),
            (Capability.CONTRACTS, FakeContractSource),
            (Capability.INVOICES, FakeInvoiceSource),
            (Capability.PAYMENTS, FakePaymentSource),
            (Capability.EXPENSES, FakeExpenseSource),
            (Capability.TICKETS, FakeTicketSource),
            (Capability.CONNECTIONS, FakeConnectionSource),
            (Capability.BANDWIDTH, FakeBandwidthUsageSource),
            (Capability.NETWORK_ELEMENTS, FakeNetworkElementSource),
            (Capability.OPTICAL_SIGNAL, FakeOpticalSignalSource),
            (Capability.EQUIPMENT, FakeEquipmentSource),
            (Capability.LEADS, FakeLeadSource),
            (Capability.OPPORTUNITIES, FakeOpportunitySource),
        ]:
            if registry.get_factory(SourceType.FAKE, cap) is None:
                registry.register(SourceType.FAKE, cap, cls)
