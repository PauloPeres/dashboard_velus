"""Enums compartilhados entre tenancy e adapters.

Vivem aqui (e não em tenancy) porque o catálogo de sistemas integrados
e capabilities suportadas é responsabilidade da camada de integração;
tenancy só REFERENCIA pra constrain o schema do OrganizationDataSource.

Ao adicionar novo adapter, atualize SourceType. Ao adicionar nova capability
ao domínio, atualize Capability.
"""

from __future__ import annotations

from django.db import models


class SourceType(models.TextChoices):
    """Sistemas externos com adapter implementado ou planejado."""

    IXC = "IXC", "IXC Soft"
    SGP = "SGP", "SGP"  # ERP alternativo p/ ISPs (placeholder)
    CONTAAZUL = "CONTAAZUL", "Conta Azul"  # accounting (placeholder)
    CSV = "CSV", "CSV upload"  # import manual (placeholder)
    FAKE = "FAKE", "Fake (testes)"  # in-memory pra dev/test


class Capability(models.TextChoices):
    """Capacidades de dados que adapters podem fornecer.

    Mapeamento com ports nos bounded contexts:
        CUSTOMERS  → apps.customers.domain.ports.CustomerSourcePort
        CONTRACTS  → apps.customers.domain.ports.ContractSourcePort
        INVOICES   → apps.financial.domain.ports.InvoiceSourcePort
        PAYMENTS   → apps.financial.domain.ports.PaymentSourcePort
        TICKETS    → apps.helpdesk.domain.ports.TicketSourcePort
    """

    CUSTOMERS = "CUSTOMERS", "Clientes"
    CONTRACTS = "CONTRACTS", "Contratos"
    INVOICES = "INVOICES", "Faturas"
    PAYMENTS = "PAYMENTS", "Pagamentos"
    EXPENSES = "EXPENSES", "Despesas"
    TICKETS = "TICKETS", "Chamados"
