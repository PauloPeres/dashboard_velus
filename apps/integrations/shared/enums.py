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
    OPA = "OPA", "Opa! Suite"  # atendimento/WhatsApp omnichannel
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
        ATENDIMENTO → apps.atendimento.domain.ports.AtendimentoSourcePort
        MENSAGENS   → idem (listagem global de mensagens)
        CONNECTIONS → apps.network.domain.ports.ConnectionSourcePort
        BANDWIDTH   → apps.network.domain.ports.BandwidthUsageSourcePort
        NETWORK_ELEMENTS → apps.network.domain.ports.NetworkElementSourcePort
        OPTICAL_SIGNAL → apps.network.domain.ports.OpticalSignalSourcePort
        EQUIPMENT  → apps.inventory.domain.ports.EquipmentSourcePort
        LEADS         → apps.sales.domain.ports.LeadSourcePort
        OPPORTUNITIES → apps.sales.domain.ports.OpportunitySourcePort
    """

    CUSTOMERS = "CUSTOMERS", "Clientes"
    CONTRACTS = "CONTRACTS", "Contratos"
    INVOICES = "INVOICES", "Faturas"
    PAYMENTS = "PAYMENTS", "Pagamentos"
    EXPENSES = "EXPENSES", "Despesas"
    TICKETS = "TICKETS", "Chamados"
    ATENDIMENTO = "ATENDIMENTO", "Atendimentos (Opa! Suite)"
    # Cursor proprio: as mensagens vem de outra listagem, com outro ritmo e
    # outro custo que os atendimentos. Compartilhar o checkpoint faria uma
    # ingestao mover o cursor da outra.
    MENSAGENS = "MENSAGENS", "Mensagens de atendimento (Opa! Suite)"
    CONNECTIONS = "CONNECTIONS", "Conexões"
    BANDWIDTH = "BANDWIDTH", "Consumo de banda"
    # Planta física (CTO, POP, PON, OLT, cabo). Capability própria e não
    # parte de CONNECTIONS porque a topologia muda em semanas e o estado do
    # login muda em minutos — compartilhar o checkpoint faria um puxar o outro.
    NETWORK_ELEMENTS = "NETWORK_ELEMENTS", "Topologia de rede"
    # Sinal óptico da ONU. Capability própria porque é a única leitura do
    # projeto que consulta equipamento de produção ao vivo (a OLT) — quem
    # habilita isso para uma organização está autorizando essa consulta, e não
    # deve vir de carona com o estado do login.
    OPTICAL_SIGNAL = "OPTICAL_SIGNAL", "Sinal óptico da ONU"
    EQUIPMENT = "EQUIPMENT", "Equipamentos em comodato"
    LEADS = "LEADS", "Leads (CRM)"
    OPPORTUNITIES = "OPPORTUNITIES", "Negociações (CRM)"
