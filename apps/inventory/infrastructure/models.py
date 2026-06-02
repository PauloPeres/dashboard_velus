"""Models de persistência do bounded context Inventory.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` é unique.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class ContractEquipment(TenantModel):
    """Equipamento em comodato (ONT, roteador, switch) emprestado a um cliente.

    `contract` é FK opcional pq sync pode receber o comodato antes do contrato
    correspondente. Repository tenta resolver via
    `(organization, source_type, contract_external_id)` no upsert.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Em campo")
        RETURNED = "RETURNED", _("Devolvido")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do comodato no sistema externo (opaco — string)."),
    )

    # FK resolvida via (source_type, contract_external_id) no Repository.
    contract = models.ForeignKey(
        "customers.Contract",
        on_delete=models.PROTECT,
        related_name="equipment",
        null=True,
        blank=True,
    )
    contract_external_id = models.CharField(max_length=128, db_index=True)

    product_name = models.CharField(max_length=255, blank=True, default="")
    serial = models.CharField(max_length=128, blank=True, default="")
    mac = models.CharField(max_length=64, blank=True, default="")
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Equipamento em comodato")
        verbose_name_plural = _("Equipamentos em comodato")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_equipment_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(
                fields=["organization", "source_type", "contract_external_id"]
            ),
            models.Index(fields=["organization", "serial"]),
        ]

    def __str__(self) -> str:
        return f"{self.product_name} [{self.status}] ({self.source_type}:{self.external_id})"
