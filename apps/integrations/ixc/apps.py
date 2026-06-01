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

        from .contracts import IxcContractSource
        from .customers import IxcCustomerSource
        from .expenses import IxcExpenseSource
        from .invoices import IxcInvoiceSource
        from .tickets import IxcTicketSource

        # Idempotência — evita duplicação em reload do dev server
        for cap, cls in [
            (Capability.CUSTOMERS, IxcCustomerSource),
            (Capability.CONTRACTS, IxcContractSource),
            (Capability.INVOICES, IxcInvoiceSource),
            (Capability.EXPENSES, IxcExpenseSource),
            (Capability.TICKETS, IxcTicketSource),
        ]:
            if registry.get_factory(SourceType.IXC, cap) is None:
                registry.register(SourceType.IXC, cap, cls)
