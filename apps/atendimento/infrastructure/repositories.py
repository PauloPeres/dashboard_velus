"""Repositorios — adaptam o ORM Django ao contrato esperado pela application layer.

Persistencia idempotente via composite unique `(organization, source_type,
external_id)` — rerodar sync nao duplica.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.atendimento.domain.dto import (
    AtendimentoDTO,
    DepartamentoDTO,
    MensagemDTO,
)
from apps.customers.domain.services import normalize_document
from apps.customers.infrastructure.models import Customer
from apps.integrations.shared.enums import SourceType
from apps.tenancy.models import Organization

from .models import Atendimento, Departamento, Mensagem


class DepartamentoRepository:
    """Persistencia idempotente de Departamento a partir de DTOs."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: DepartamentoDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Departamento, bool]:
        defaults: dict[str, Any] = {
            "nome": dto.nome or "",
            "status": dto.status or "",
            "raw_extras": dto.raw_extras,
        }
        return Departamento.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )


class AtendimentoRepository:
    """Persistencia idempotente de Atendimento a partir de DTOs.

    Resolve duas FKs opcionais no upsert:
    - `customer` via `(organization, document)` — ponte logica cross-source.
    - `departamento` via `(organization, source_type, departamento_external_id)`.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: AtendimentoDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Atendimento, bool]:
        document = normalize_document(dto.customer_document)
        customer = None
        if document:
            customer = (
                Customer.objects
                .filter(organization=self.organization, document=document)
                .first()
            )

        departamento = None
        if dto.departamento_external_id:
            departamento = (
                Departamento.objects
                .filter(
                    organization=self.organization,
                    source_type=source_type.value,
                    external_id=dto.departamento_external_id,
                )
                .first()
            )

        defaults: dict[str, Any] = {
            "customer": customer,
            "customer_external_id": dto.customer_external_id or "",
            "customer_document": document,
            "customer_name": dto.customer_name or "",
            "departamento": departamento,
            "departamento_external_id": dto.departamento_external_id or "",
            "atendente_external_id": dto.atendente_external_id or "",
            "atendente_nome": dto.atendente_nome or "",
            "status": self._normalize_status(dto.status),
            "canal": dto.canal or "",
            "protocol": dto.protocol or "",
            "motivos": list(dto.motivos or []),
            "rating": dto.rating,
            "opened_at": dto.opened_at,
            "closed_at": dto.closed_at,
            "raw_extras": dto.raw_extras,
        }
        return Atendimento.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Atendimento.Status.values:
            return raw_upper
        return Atendimento.Status.UNKNOWN.value


class MensagemRepository:
    """Persistencia idempotente de Mensagem a partir de DTOs."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: MensagemDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Mensagem, bool]:
        atendimento = (
            Atendimento.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.atendimento_external_id,
            )
            .first()
        )
        defaults: dict[str, Any] = {
            "atendimento": atendimento,
            "atendimento_external_id": dto.atendimento_external_id,
            "direction": self._normalize_direction(dto.direction),
            "tipo": dto.tipo or "",
            "texto": dto.texto or "",
            "sent_at": dto.sent_at,
            "raw_extras": dto.raw_extras,
        }
        return Mensagem.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )

    @staticmethod
    def _normalize_direction(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Mensagem.Direction.values:
            return raw_upper
        return Mensagem.Direction.UNKNOWN.value
