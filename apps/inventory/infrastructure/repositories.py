"""Repositórios — adaptam o ORM Django ao contrato esperado pela application layer."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.customers.infrastructure.models import Contract
from apps.integrations.shared.enums import SourceType
from apps.inventory.domain.dto import EquipmentDTO
from apps.tenancy.models import Organization

from .models import ContractEquipment


class EquipmentRepository:
    """Persistência idempotente de ContractEquipment a partir de DTOs.

    Idempotência via composite unique `(organization, source_type, external_id)` —
    upsert atualiza se já existe, cria se não. Rerodar sync não duplica.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: EquipmentDTO,
        *,
        source_type: SourceType,
    ) -> tuple[ContractEquipment, bool]:
        """Upsert idempotente. Retorna (equipment, created)."""
        contract = (
            Contract.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.contract_external_id,
            )
            .first()
            if dto.contract_external_id
            else None
        )

        defaults: dict[str, Any] = {
            "contract": contract,
            "contract_external_id": dto.contract_external_id,
            "product_name": dto.product_name,
            "serial": dto.serial,
            "mac": dto.mac,
            "value": dto.value,
            "status": self._normalize_status(dto.status),
            "raw_extras": dto.raw_extras,
        }
        equipment, created = ContractEquipment.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return equipment, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in ContractEquipment.Status.values:
            return raw_upper
        return ContractEquipment.Status.UNKNOWN.value
