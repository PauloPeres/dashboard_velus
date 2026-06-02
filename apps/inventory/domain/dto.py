"""DTOs do domínio Inventory — neutros, sem campos source-specific."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class EquipmentDTO:
    """Representação neutra de um equipamento em comodato (emprestado ao cliente).

    `external_id` é opaco — string que identifica o comodato no sistema de origem.
    Combinado com `source_type` (que vive no adapter), forma a chave composta de
    persistência: `(organization, source_type, external_id)`.

    `contract_external_id` é o snapshot do contrato dono do equipamento — o
    Repository tenta resolver a FK via `(organization, source_type, contract_external_id)`.

    `status` deriva do estado do comodato na origem:
    ACTIVE (em campo com o cliente), RETURNED (devolvido), UNKNOWN.
    """

    external_id: str
    contract_external_id: str
    product_name: str
    status: str  # ACTIVE, RETURNED, UNKNOWN

    serial: str = ""
    mac: str = ""
    value: Decimal = Decimal("0")

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("EquipmentDTO.external_id não pode ser vazio")
