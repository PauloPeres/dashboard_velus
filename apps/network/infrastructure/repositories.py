"""Repositórios — adaptam o ORM Django ao contrato esperado pela application layer."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from apps.customers.infrastructure.models import Customer
from apps.integrations.shared.enums import SourceType
from apps.network.domain.dto import ConnectionDTO
from apps.tenancy.models import Organization

from .models import Connection


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
            "raw_extras": dto.raw_extras,
        }
        connection, created = Connection.objects.update_or_create(
            organization=self.organization,
            source_type=source_type.value,
            external_id=dto.external_id,
            defaults=defaults,
        )
        return connection, created

    @staticmethod
    def _normalize_status(raw: str) -> str:
        raw_upper = (raw or "").upper().strip()
        if raw_upper in Connection.Status.values:
            return raw_upper
        return Connection.Status.UNKNOWN.value
