"""Models de persistencia do bounded context Helpdesk.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` e unique.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Ticket(TenantModel):
    """Chamado de suporte vindo de uma fonte externa (IXC, ...).

    `customer` e FK opcional pq sync pode receber chamados antes do cliente
    correspondente. Repository tenta resolver via
    `(organization, source_type, customer_external_id)` no upsert.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Aberto")
        SCHEDULED = "SCHEDULED", _("Agendado")
        IN_PROGRESS = "IN_PROGRESS", _("Em execucao")
        CLOSED = "CLOSED", _("Fechado")
        FORWARDED = "FORWARDED", _("Encaminhado")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    class Priority(models.TextChoices):
        NORMAL = "NORMAL", _("Normal")
        HIGH = "HIGH", _("Alta")
        LOW = "LOW", _("Baixa")
        URGENT = "URGENT", _("Urgente")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do chamado no sistema externo (opaco — string)."),
    )

    # FK resolvida via (source_type, customer_external_id) no Repository.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="tickets",
        null=True,
        blank=True,
    )
    customer_external_id = models.CharField(max_length=128, db_index=True)

    subject_id = models.CharField(max_length=128, blank=True, default="")
    sector = models.CharField(max_length=128, blank=True, default="")
    technician_id = models.CharField(max_length=128, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )
    priority = models.CharField(
        max_length=16, choices=Priority.choices, default=Priority.UNKNOWN
    )
    message = models.TextField(blank=True, default="")
    protocol = models.CharField(max_length=128, blank=True, default="")

    opened_at = models.DateTimeField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Chamado")
        verbose_name_plural = _("Chamados")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_ticket_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "opened_at"]),
            models.Index(fields=["organization", "source_type", "customer_external_id"]),
        ]

    def __str__(self) -> str:
        return f"#{self.protocol} ({self.source_type}:{self.external_id})"


# =============================================================================
# Lookups de OS IXC — cache sincronizado via `sync_os_lookups`
# =============================================================================
class OsLookupCache(models.Model):
    """Cache de nomes de assunto e técnico das OS IXC para uma organização.

    As OS guardam dimensões como IDs opacos (`Ticket.subject_id`, `Ticket.technician_id`).
    Este cache resolve esses IDs em nomes legíveis pros dashboards de OS:

    - subject_map: {id_assunto → nome}   (endpoint IXC `su_oss_assunto`)
    - technician_map: {id_tecnico → nome} (endpoint IXC `funcionarios`)

    Atualizado via `python manage.py sync_os_lookups <org_slug>`.
    """

    organization = models.OneToOneField(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="os_lookup_cache",
        verbose_name=_("Organização"),
    )
    # {id_assunto (str) → nome}
    subject_map = models.JSONField(
        default=dict,
        verbose_name=_("Mapa de assuntos"),
        help_text="su_oss_assunto.id → assunto (tipo de OS)",
    )
    # {id_tecnico (str) → nome}
    technician_map = models.JSONField(
        default=dict,
        verbose_name=_("Mapa de técnicos"),
        help_text="funcionarios.id → funcionario (nome do técnico)",
    )
    synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Última sincronização"),
    )

    class Meta:
        verbose_name = _("Cache de lookups de OS IXC")
        verbose_name_plural = _("Caches de lookups de OS IXC")

    def __str__(self) -> str:
        return (
            f"OsLookupCache {self.organization_id} · "
            f"{len(self.subject_map)} assuntos, {len(self.technician_map)} técnicos"
        )
