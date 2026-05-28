"""Repositórios — adaptam o ORM Django ao contrato esperado pela application layer.

Repository NÃO conhece TenantManager — confia que o caller setou contexto antes.
Em testes, fixtures de pytest setam contextvar manualmente.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.customers.domain.services import normalize_document
from apps.integrations.shared.enums import SourceType
from apps.tenancy.models import Organization

from .models import Contract, Customer


class CustomerRepository:
    """Persistência idempotente de Customer a partir de DTOs.

    Idempotência via composite unique `(organization, source_type, external_id)` —
    upsert atualiza se já existe, cria se não. Rerodar sync não duplica.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: CustomerDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Customer, bool]:
        """Upsert idempotente. Retorna (customer, created).

        Mudanças no `status` ou `name` são versionadas via simple_history.
        """
        defaults: dict[str, Any] = {
            "document": normalize_document(dto.document),
            "name": dto.name,
            "email": dto.email or "",
            "phone": dto.phone or "",
            "status": self._normalize_status(dto.status),
            "created_at_source": dto.created_at_source,
            "raw_extras": dto.raw_extras,
        }
        customer, created = Customer.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return customer, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Customer.Status.values:
            return raw_upper
        return Customer.Status.UNKNOWN.value


class ContractRepository:
    """Persistência idempotente de Contract a partir de DTOs.

    Resolução de FK pra Customer:
    - Procura Customer por `(organization, source_type, customer_external_id)`.
    - Se encontra: FK seta. Se não: contrato persiste com `customer=NULL`
      (visível em admin pra investigação). Próximo sync de Customers + rerun
      do sync de Contracts resolve.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: ContractDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Contract, bool]:
        customer = (
            Customer.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.customer_external_id,
            )
            .first()
        )

        defaults: dict[str, Any] = {
            "customer": customer,
            "customer_external_id": dto.customer_external_id,
            "plan_name": dto.plan_name,
            "monthly_amount": dto.monthly_amount,
            "status": self._normalize_status(dto.status),
            "activated_at": dto.activated_at,
            "canceled_at": dto.canceled_at,
            "address": dto.address or "",
            "raw_extras": dto.raw_extras,
        }
        contract, created = Contract.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return contract, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Contract.Status.values:
            return raw_upper
        return Contract.Status.UNKNOWN.value
