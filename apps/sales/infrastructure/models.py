"""Models de persistência do bounded context Sales/CRM.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` é unique.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Lead(TenantModel):
    """Lead/prospect do funil de vendas (CRM)."""

    class Status(models.TextChoices):
        NEW = "NEW", _("Novo")
        CONTACTED = "CONTACTED", _("Em contato")
        CONVERTED = "CONVERTED", _("Convertido")
        LOST = "LOST", _("Perdido")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do candidato no sistema externo (opaco — string)."),
    )

    name = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    origin = models.CharField(
        max_length=128, blank=True, default="",
        help_text=_("Canal de origem (indicação, site, redes sociais...)."),
    )
    salesperson_id = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )

    created_at_source = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Lead")
        verbose_name_plural = _("Leads")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_lead_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "created_at_source"]),
            models.Index(fields=["organization", "origin"]),
        ]

    def __str__(self) -> str:
        return f"{self.name or 'Lead'} [{self.status}] ({self.source_type}:{self.external_id})"


class Opportunity(TenantModel):
    """Negociação/oportunidade de venda atrelada a um lead (CRM).

    `lead` é FK opcional pq sync pode receber a negociação antes do lead
    correspondente. Repository tenta resolver via
    `(organization, source_type, lead_external_id)` no upsert.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Em andamento")
        WON = "WON", _("Ganha")
        LOST = "LOST", _("Perdida")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    external_id = models.CharField(max_length=128)

    # FK resolvida via (source_type, lead_external_id) no Repository.
    lead = models.ForeignKey(
        "sales.Lead",
        on_delete=models.PROTECT,
        related_name="opportunities",
        null=True,
        blank=True,
    )
    lead_external_id = models.CharField(max_length=128, db_index=True)

    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )
    loss_reason = models.CharField(max_length=255, blank=True, default="")

    created_at_source = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Negociação")
        verbose_name_plural = _("Negociações")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_opportunity_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "created_at_source"]),
            models.Index(fields=["organization", "source_type", "lead_external_id"]),
        ]

    def __str__(self) -> str:
        return f"Negociação {self.value} [{self.status}] ({self.source_type}:{self.external_id})"
