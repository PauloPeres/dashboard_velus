"""Models de persistência do bounded context Network.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` é unique.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Connection(TenantModel):
    """Estado de conexão de um cliente (RADIUS/PPPoE) vindo de fonte externa.

    `customer` é FK opcional pq sync pode receber conexões antes do cliente
    correspondente. Repository tenta resolver via
    `(organization, source_type, customer_external_id)` no upsert.
    """

    class Status(models.TextChoices):
        ONLINE = "ONLINE", _("Online")
        OFFLINE = "OFFLINE", _("Offline")
        BLOCKED = "BLOCKED", _("Bloqueado")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID da conexão no sistema externo (opaco — string)."),
    )

    # FK resolvida via (source_type, customer_external_id) no Repository.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="connections",
        null=True,
        blank=True,
    )
    customer_external_id = models.CharField(max_length=128, db_index=True)
    contract_external_id = models.CharField(max_length=128, blank=True, default="")

    login = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )

    ip = models.CharField(max_length=64, blank=True, default="")
    nas_ip = models.CharField(max_length=64, blank=True, default="")
    rx_bytes = models.BigIntegerField(default=0)
    tx_bytes = models.BigIntegerField(default=0)
    download_speed = models.CharField(max_length=64, blank=True, default="")
    upload_speed = models.CharField(max_length=64, blank=True, default="")

    last_connection_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Conexão")
        verbose_name_plural = _("Conexões")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_connection_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "last_connection_at"]),
            models.Index(fields=["organization", "nas_ip"]),
            models.Index(
                fields=["organization", "source_type", "customer_external_id"]
            ),
        ]

    def __str__(self) -> str:
        return f"{self.login} [{self.status}] ({self.source_type}:{self.external_id})"
