"""Repositorios — adaptam o ORM Django ao contrato esperado pela application layer.

Persistencia idempotente via composite unique `(organization, source_type,
external_id)` — rerodar sync nao duplica.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.atendimento.domain.dto import (
    AtendimentoDTO,
    CanalComunicacaoDTO,
    DepartamentoDTO,
    EtiquetaDTO,
    MensagemDTO,
    MotivoDTO,
)
from apps.customers.domain.services import normalize_document
from apps.customers.infrastructure.models import Customer
from apps.integrations.shared.enums import SourceType
from apps.tenancy.models import Organization

from .models import (
    Atendimento,
    CanalComunicacao,
    Departamento,
    Etiqueta,
    Mensagem,
    Motivo,
)


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


class EtiquetaRepository:
    """Persistencia idempotente de Etiqueta (catalogo de tags) a partir de DTOs."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: EtiquetaDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Etiqueta, bool]:
        defaults: dict[str, Any] = {
            "nome": dto.nome or "",
            "cor": dto.cor or "",
            "raw_extras": dto.raw_extras,
        }
        return Etiqueta.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )


class MotivoRepository:
    """Persistencia idempotente de Motivo (catalogo de motivos) a partir de DTOs."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: MotivoDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Motivo, bool]:
        defaults: dict[str, Any] = {
            "nome": dto.nome or "",
            "raw_extras": dto.raw_extras,
        }
        return Motivo.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )


class CanalComunicacaoRepository:
    """Persistencia idempotente de CanalComunicacao (catalogo de canais)."""

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: CanalComunicacaoDTO,
        *,
        source_type: SourceType,
    ) -> tuple[CanalComunicacao, bool]:
        defaults: dict[str, Any] = {
            "nome": dto.nome or "",
            "canal": dto.canal or "",
            "integracao": dto.integracao or "",
            "status": dto.status or "",
            "raw_extras": dto.raw_extras,
        }
        return CanalComunicacao.objects.update_or_create(
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
            "canal_external_id": dto.canal_external_id or "",
            "origem_tipo": dto.origem_tipo or "",
            "origem_ref": dto.origem_ref or "",
            "protocol": dto.protocol or "",
            "motivos": list(dto.motivos or []),
            "tags": list(dto.tags or []),
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
            "canal_external_id": dto.canal_external_id or "",
            "fora_janela_24h": dto.fora_janela_24h,
            "sent_at": dto.sent_at,
            "raw_extras": dto.raw_extras,
        }
        return Mensagem.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )

    @transaction.atomic
    def bulk_upsert_metadata(
        self,
        dtos: list[MensagemDTO],
        *,
        source_type: SourceType,
    ) -> int:
        """Grava um lote de mensagens (sem texto) em uma ida ao banco.

        Existe porque o backfill de volume processa centenas de milhares de
        registros: `update_or_create` por mensagem seriam 2 queries cada. Aqui
        e 1 SELECT pros atendimentos do lote + 1 INSERT ... ON CONFLICT DO
        UPDATE, o que mantem a idempotencia do upsert normal.

        `texto` fica de fora do `update_fields` de proposito: reprocessar o
        backfill nao pode apagar o texto que o drill-down ja buscou.
        """
        if not dtos:
            return 0

        externos = {d.atendimento_external_id for d in dtos if d.atendimento_external_id}
        por_externo = dict(
            Atendimento.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id__in=externos,
            )
            .values_list("external_id", "id")
        )

        objs = [
            Mensagem(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.external_id,
                atendimento_id=por_externo.get(dto.atendimento_external_id),
                atendimento_external_id=dto.atendimento_external_id or "",
                direction=self._normalize_direction(dto.direction),
                tipo=dto.tipo or "",
                texto="",
                canal_external_id=dto.canal_external_id or "",
                fora_janela_24h=dto.fora_janela_24h,
                sent_at=dto.sent_at,
                raw_extras={},
            )
            for dto in dtos
        ]
        Mensagem.objects.bulk_create(
            objs,
            update_conflicts=True,
            unique_fields=["organization", "source_type", "external_id"],
            update_fields=[
                "atendimento",
                "atendimento_external_id",
                "direction",
                "tipo",
                "canal_external_id",
                "fora_janela_24h",
                "sent_at",
            ],
        )
        return len(objs)

    @staticmethod
    def _normalize_direction(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Mensagem.Direction.values:
            return raw_upper
        return Mensagem.Direction.UNKNOWN.value
