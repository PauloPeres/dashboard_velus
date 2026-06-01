"""Repositorios — adaptam o ORM Django ao contrato esperado pela application layer."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.customers.infrastructure.models import Customer
from apps.helpdesk.domain.dto import TicketDTO
from apps.integrations.shared.enums import SourceType
from apps.tenancy.models import Organization

from .models import Ticket


class TicketRepository:
    """Persistencia idempotente de Ticket a partir de DTOs.

    Idempotencia via composite unique `(organization, source_type, external_id)` —
    upsert atualiza se ja existe, cria se nao. Rerodar sync nao duplica.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: TicketDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Ticket, bool]:
        """Upsert idempotente. Retorna (ticket, created)."""
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
            "subject_id": dto.subject_id,
            "sector": dto.sector,
            "technician_id": dto.technician_id,
            "status": self._normalize_status(dto.status),
            "priority": self._normalize_priority(dto.priority),
            "message": dto.message or "",
            "protocol": dto.protocol or "",
            "opened_at": dto.opened_at,
            "scheduled_at": dto.scheduled_at,
            "closed_at": dto.closed_at,
            "raw_extras": dto.raw_extras,
        }
        ticket, created = Ticket.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return ticket, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Ticket.Status.values:
            return raw_upper
        return Ticket.Status.UNKNOWN.value

    @staticmethod
    def _normalize_priority(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Ticket.Priority.values:
            return raw_upper
        return Ticket.Priority.UNKNOWN.value
