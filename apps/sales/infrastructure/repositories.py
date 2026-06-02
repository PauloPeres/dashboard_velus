"""Repositórios — adaptam o ORM Django ao contrato esperado pela application layer."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.integrations.shared.enums import SourceType
from apps.sales.domain.dto import LeadDTO, OpportunityDTO
from apps.tenancy.models import Organization

from .models import Lead, Opportunity


class LeadRepository:
    """Persistência idempotente de Lead a partir de DTOs.

    Idempotência via composite unique `(organization, source_type, external_id)` —
    upsert atualiza se já existe, cria se não. Rerodar sync não duplica.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: LeadDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Lead, bool]:
        """Upsert idempotente. Retorna (lead, created)."""
        defaults: dict[str, Any] = {
            "name": dto.name,
            "phone": dto.phone,
            "email": dto.email,
            "origin": dto.origin,
            "salesperson_id": dto.salesperson_id,
            "status": self._normalize_status(dto.status),
            "created_at_source": dto.created_at_source,
            "raw_extras": dto.raw_extras,
        }
        lead, created = Lead.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return lead, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Lead.Status.values:
            return raw_upper
        return Lead.Status.UNKNOWN.value


class OpportunityRepository:
    """Persistência idempotente de Opportunity a partir de DTOs.

    Resolve a FK `lead` via `(organization, source_type, lead_external_id)` no
    momento do upsert; FK nullable pra não bloquear quando a ordem de sync
    (Leads -> Opportunities) inverte.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: OpportunityDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Opportunity, bool]:
        """Upsert idempotente. Retorna (opportunity, created)."""
        lead = (
            Lead.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.lead_external_id,
            )
            .first()
            if dto.lead_external_id
            else None
        )

        defaults: dict[str, Any] = {
            "lead": lead,
            "lead_external_id": dto.lead_external_id,
            "value": dto.value,
            "status": self._normalize_status(dto.status),
            "loss_reason": dto.loss_reason,
            "created_at_source": dto.created_at_source,
            "closed_at": dto.closed_at,
            "raw_extras": dto.raw_extras,
        }
        opportunity, created = Opportunity.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return opportunity, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Opportunity.Status.values:
            return raw_upper
        return Opportunity.Status.UNKNOWN.value
