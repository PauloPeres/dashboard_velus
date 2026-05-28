"""DTOs do domínio de Customers — neutros, sem campos source-specific.

Princípio (AGENT.md §1.6 #2): campos comuns entre fontes (IXC, ContaAzul, etc.)
ficam aqui explícitos. Campos específicos vão em `raw_extras: dict` opaco,
que o domain NÃO ACESSA (só persiste pra debug/audit no DB).

Quando um campo vira essencial pra >1 fonte, promove pro DTO — operação
deliberada que envolve atualizar adapters e migration de Customer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CustomerDTO:
    """Representação neutra de um cliente vinda de qualquer fonte externa.

    `external_id` é opaco — string que identifica o cliente no sistema de origem.
    Combinado com `source_type` (que vive no adapter, não aqui), forma a chave
    composta de persistência: `(organization, source_type, external_id)`.

    `document` (CPF/CNPJ, dígitos apenas) é o ponto de fusão lógica quando o
    mesmo cliente físico existe em múltiplas fontes — ver
    `apps.customers.domain.services.resolve_identity`.
    """

    external_id: str
    document: str  # CPF (11 dígitos) ou CNPJ (14 dígitos), só números
    name: str
    status: str = "ACTIVE"  # ACTIVE | BLOCKED | CANCELED — normalizado pelo adapter

    email: str | None = None
    phone: str | None = None

    # Datas opcionais — adapters preenchem o que conseguirem do sistema externo
    created_at_source: datetime | None = None  # quando o cliente foi criado no sistema fonte

    # Campos específicos do sistema fonte que ainda não são gerais o bastante
    # pra entrar no DTO. Não acessados por código de domain.
    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Normalização mínima — sem regex pesada (responsabilidade do adapter
        # entregar `document` só com dígitos)
        if not self.external_id:
            raise ValueError("CustomerDTO.external_id não pode ser vazio")
        if not self.name:
            raise ValueError("CustomerDTO.name não pode ser vazio")


@dataclass(frozen=True)
class ContractDTO:
    """Representação neutra de contrato (assinatura recorrente).

    `customer_external_id` é o ID do cliente NO MESMO sistema-fonte.
    Repository resolve a FK pra Customer via `(source_type, customer_external_id)`.
    Se o cliente referenciado ainda não existe na base, contrato é persistido
    sem FK (Customer FK nullable) — sync de Customers deve preceder Contracts.
    """

    external_id: str
    customer_external_id: str
    plan_name: str
    monthly_amount: Decimal
    status: str = "ACTIVE"  # ACTIVE | BLOCKED | CANCELED | AWAITING_INSTALL

    activated_at: datetime | None = None
    canceled_at: datetime | None = None
    address: str | None = None  # endereço de instalação (opcional)

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("ContractDTO.external_id não pode ser vazio")
        if not self.customer_external_id:
            raise ValueError("ContractDTO.customer_external_id não pode ser vazio")
        # Coage int/str pra Decimal por segurança (adapters podem passar str)
        if not isinstance(self.monthly_amount, Decimal):
            object.__setattr__(self, "monthly_amount", Decimal(str(self.monthly_amount)))
