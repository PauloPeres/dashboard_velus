"""DTOs do dominio Atendimento — neutros, sem campos source-specific.

Modela conversas/atendimentos omnichannel (Opa! Suite hoje). `external_id` e
opaco — string que identifica o registro na fonte; combinado com `source_type`
(que vive no adapter) forma a chave composta `(organization, source_type,
external_id)`.

O vinculo conversa->cliente e feito por **documento (CPF/CNPJ)**, nao pelo id
opaco do cliente na fonte: o id de cliente da Opa (`customer_external_id`) nao
bate com o `external_id` do Customer no IXC. `customer_document` e a ponte
logica cross-source (igual ao `Customer.document`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DepartamentoDTO:
    """Setor/departamento de atendimento (Comercial, Suporte, ...)."""

    external_id: str
    nome: str
    status: str = ""
    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("DepartamentoDTO.external_id nao pode ser vazio")


@dataclass(frozen=True)
class ClienteRefDTO:
    """Referencia leve de cliente na fonte — mapeia id opaco -> documento.

    Usado pra construir o mapa `customer_external_id -> document` a partir da
    lista barata de clientes da fonte, sem popular atendimento-a-atendimento.
    """

    external_id: str
    document: str
    nome: str = ""

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("ClienteRefDTO.external_id nao pode ser vazio")


@dataclass(frozen=True)
class AtendenteRefDTO:
    """Referencia leve de atendente/usuario na fonte — mapeia id opaco -> nome.

    Usado pra preencher `atendente_nome` no atendimento: a listagem de
    atendimentos so traz o atendente como id opaco, sem o nome.
    """

    external_id: str
    nome: str = ""

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("AtendenteRefDTO.external_id nao pode ser vazio")


@dataclass(frozen=True)
class AtendimentoDTO:
    """Representacao neutra de um atendimento/conversa de qualquer fonte externa."""

    external_id: str
    customer_external_id: str  # id opaco do cliente na fonte (Opa)
    customer_document: str  # CPF/CNPJ normalizado — ponte logica pro Customer
    customer_name: str
    departamento_external_id: str
    atendente_external_id: str
    atendente_nome: str
    status: str  # OPEN, IN_PROGRESS, CLOSED
    canal: str  # whatsapp, ...
    protocol: str
    opened_at: datetime | None

    motivos: list[str] = field(default_factory=list)
    rating: int | None = None  # nota humana likert 1-5 (so vem em GET populado)
    closed_at: datetime | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("AtendimentoDTO.external_id nao pode ser vazio")


@dataclass(frozen=True)
class MensagemDTO:
    """Mensagem trocada dentro de um atendimento.

    `direction` neutraliza o `tipoDestinatario` da Opa: CLIENT (cliente/bot),
    AGENT (atendente humano), SYSTEM (eventos).
    """

    external_id: str
    atendimento_external_id: str
    direction: str  # CLIENT, AGENT, SYSTEM
    tipo: str  # texto, menuInterativo, ...
    texto: str
    sent_at: datetime | None
    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("MensagemDTO.external_id nao pode ser vazio")
