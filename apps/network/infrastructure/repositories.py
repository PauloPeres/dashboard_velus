"""Repositórios — adaptam o ORM Django ao contrato esperado pela application layer."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.customers.infrastructure.models import Customer
from apps.integrations.shared.enums import SourceType
from apps.network.domain.dto import (
    BandwidthUsageDTO,
    ConnectionDTO,
    ElementGeometryDTO,
    NetworkElementDTO,
)
from apps.tenancy.models import Organization

from .models import (
    BandwidthUsage,
    Connection,
    NetworkElement,
    NetworkElementGeometry,
)


class ConnectionRepository:
    """Persistência idempotente de Connection a partir de DTOs.

    Idempotência via composite unique `(organization, source_type, external_id)` —
    upsert atualiza se já existe, cria se não. Rerodar sync não duplica.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: ConnectionDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Connection, bool]:
        """Upsert idempotente. Retorna (connection, created)."""
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
            "contract_external_id": dto.contract_external_id,
            "login": dto.login,
            "status": self._normalize_status(dto.status),
            "ip": dto.ip,
            "nas_ip": dto.nas_ip,
            "rx_bytes": dto.rx_bytes,
            "tx_bytes": dto.tx_bytes,
            "download_speed": dto.download_speed,
            "upload_speed": dto.upload_speed,
            "last_connection_at": dto.last_connection_at,
            "cto_external_id": dto.cto_external_id,
            "cto_port": dto.cto_port,
            "pon_external_id": dto.pon_external_id,
            "onu_external_id": dto.onu_external_id,
            "transmitter_external_id": dto.transmitter_external_id,
            "concentrator_external_id": dto.concentrator_external_id,
            "latitude": dto.latitude,
            "longitude": dto.longitude,
            "disconnect_reason": dto.disconnect_reason,
            "last_disconnection_at": dto.last_disconnection_at,
            "raw_extras": dto.raw_extras,
        }
        connection, created = Connection.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return connection, created

    @transaction.atomic
    def apply_status_snapshot(
        self,
        dto: ConnectionDTO,
        *,
        source_type: SourceType,
    ) -> tuple[Connection, bool, str]:
        """Aplica a leitura do poll de 3 min. Retorna (connection, created, status_anterior).

        Difere do `upsert_from_dto` em duas coisas, e as duas importam:

        1. devolve o **status anterior**, que é o que sustenta a partida a frio —
           depois de gravar já não dá pra saber se o login estava no ar;
        2. campo vazio no DTO **não apaga** o que está no banco. O poll lê só
           `radusuarios` e não paga o enriquecimento de ONU, então a PON vem
           vazia; se sobrescrevesse, o detector perderia o degrau de PON até o
           próximo sync de 6h.
        """
        existing = (
            Connection.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.external_id,
            )
            .first()
        )
        if existing is None:
            connection, created = self.upsert_from_dto(dto, source_type=source_type)
            # Login que o sync completo ainda não viu: sem observação anterior,
            # esta leitura é linha de base e não queda (§5.7).
            return connection, created, ""

        previous_status = existing.status
        novos: dict[str, Any] = {
            "status": self._normalize_status(dto.status),
            "ip": dto.ip,
            "last_connection_at": dto.last_connection_at,
            "last_disconnection_at": dto.last_disconnection_at,
            "disconnect_reason": dto.disconnect_reason,
        }
        for field in (
            "cto_external_id",
            "cto_port",
            "pon_external_id",
            "onu_external_id",
            "transmitter_external_id",
            "concentrator_external_id",
        ):
            value = getattr(dto, field)
            if value:
                novos[field] = value
        if dto.latitude is not None and dto.longitude is not None:
            novos["latitude"] = dto.latitude
            novos["longitude"] = dto.longitude

        # Só grava se algo mudou de fato. Connection tem HistoricalRecords: um
        # save por login offline a cada 3 min seriam ~113 mil linhas de
        # histórico por dia pra registrar que nada aconteceu.
        changed = [f for f, v in novos.items() if getattr(existing, f) != v]
        if not changed:
            return existing, False, previous_status
        for field in changed:
            setattr(existing, field, novos[field])
        existing.save(update_fields=[*changed, "updated_at"])
        return existing, False, previous_status

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Connection.Status.values:
            return raw_upper
        return Connection.Status.UNKNOWN.value


class BandwidthUsageRepository:
    """Persistência idempotente de BandwidthUsage a partir de DTOs.

    Idempotência via composite unique `(organization, source_type, external_id)` —
    upsert atualiza se já existe, cria se não. Rerodar sync não duplica.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: BandwidthUsageDTO,
        *,
        source_type: SourceType,
    ) -> tuple[BandwidthUsage, bool]:
        """Upsert idempotente. Retorna (usage, created)."""
        customer = (
            Customer.objects
            .filter(
                organization=self.organization,
                source_type=source_type.value,
                external_id=dto.customer_external_id,
            )
            .first()
            if dto.customer_external_id
            else None
        )

        defaults: dict[str, Any] = {
            "customer": customer,
            "customer_external_id": dto.customer_external_id,
            "download_bytes": dto.download_bytes,
            "upload_bytes": dto.upload_bytes,
            "session_time": dto.session_time,
            "reference_date": dto.reference_date,
            "raw_extras": dto.raw_extras,
        }
        usage, created = BandwidthUsage.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return usage, created


class NetworkElementGeometryRepository:
    """Persistência idempotente do traçado de um elemento.

    Mesma chave composta do elemento — `(organization, source_type, kind,
    external_id)` —, porque é o mesmo objeto visto de outro ângulo.

    O traçado é **substituído**, nunca acumulado: cabo que perdeu vértice no
    cadastro tem que perder aqui também. Acumular faria a linha crescer para
    sempre e desenhar um caminho que o projeto não tem mais.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: ElementGeometryDTO,
        *,
        source_type: SourceType,
    ) -> tuple[NetworkElementGeometry, bool]:
        """Upsert idempotente. Retorna (geometry, created)."""
        geometry, created = NetworkElementGeometry.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            kind=dto.kind,
            external_id=dto.external_id,
            defaults={
                "name": dto.name,
                "project_external_id": dto.project_external_id,
                # Lista de listas, não de tuplas: o JSONField devolveria listas
                # na próxima leitura de qualquer jeito, e gravar tupla faria o
                # objeto em memória divergir do que o banco entrega.
                "points": [[lat, lon] for lat, lon in dto.points],
            },
        )
        return geometry, created


class NetworkElementRepository:
    """Persistência idempotente de NetworkElement a partir de DTOs.

    Idempotência via composite unique `(organization, source_type, kind,
    external_id)`. `kind` entra na chave porque os ids do IXC são sequências por
    tabela: a caixa 12 e o POP 12 existem os dois.
    """

    def __init__(self, organization: Organization) -> None:
        self.organization = organization

    @transaction.atomic
    def upsert_from_dto(
        self,
        dto: NetworkElementDTO,
        *,
        source_type: SourceType,
    ) -> tuple[NetworkElement, bool]:
        """Upsert idempotente. Retorna (element, created)."""
        defaults: dict[str, Any] = {
            "name": dto.name,
            "latitude": dto.latitude,
            "longitude": dto.longitude,
            "parent_external_id": dto.parent_external_id,
            "parent_kind": dto.parent_kind,
            "capacity": dto.capacity,
            "address": dto.address,
            "project_external_id": dto.project_external_id,
            "status": dto.status,
            "raw_extras": dto.raw_extras,
        }
        element, created = NetworkElement.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            kind=dto.kind,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return element, created
