"""AppConfig do adapter IXC Soft.

Registra `IxcCustomerSource` no SourceRegistry quando o app é carregado.
"""

from __future__ import annotations

from django.apps import AppConfig


class IxcAdapterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations.ixc"
    label = "integrations_ixc"
    verbose_name = "Integrações: IXC Soft"

    def ready(self) -> None:
        from apps.integrations.shared.enums import Capability, SourceType
        from apps.integrations.shared.registry import registry

        from .bandwidth import IxcBandwidthUsageSource
        from .connections import IxcConnectionSource
        from .contracts import IxcContractSource
        from .customers import IxcCustomerSource
        from .equipment import IxcEquipmentSource
        from .expenses import IxcExpenseSource
        from .invoices import IxcInvoiceSource
        from .leads import IxcLeadSource
        from .opportunities import IxcOpportunitySource
        from .payments import IxcPaymentSource
        from .tickets import IxcTicketSource

        # Idempotência — evita duplicação em reload do dev server
        for cap, cls in [
            (Capability.CUSTOMERS, IxcCustomerSource),
            (Capability.CONTRACTS, IxcContractSource),
            (Capability.INVOICES, IxcInvoiceSource),
            (Capability.PAYMENTS, IxcPaymentSource),
            (Capability.EXPENSES, IxcExpenseSource),
            (Capability.TICKETS, IxcTicketSource),
            (Capability.CONNECTIONS, IxcConnectionSource),
            (Capability.BANDWIDTH, IxcBandwidthUsageSource),
            (Capability.EQUIPMENT, IxcEquipmentSource),
            (Capability.LEADS, IxcLeadSource),
            (Capability.OPPORTUNITIES, IxcOpportunitySource),
        ]:
            if registry.get_factory(SourceType.IXC, cap) is None:
                registry.register(SourceType.IXC, cap, cls)
