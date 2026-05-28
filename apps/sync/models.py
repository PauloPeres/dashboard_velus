"""Models de orquestração de sync.

Sync app NÃO conhece IXC nem qualquer adapter — conversa com ports via registry.
Estes models são puramente operacionais (jobs, checkpoints, status).

Os 3 records existem por `(organization, source_type, capability)` —
permite múltiplas fontes da mesma capability rodando independente.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.integrations.shared.enums import Capability, SourceType


class SyncMode(models.TextChoices):
    BOOTSTRAP = "BOOTSTRAP", _("Bootstrap (carga total inicial)")
    INCREMENTAL = "INCREMENTAL", _("Incremental (delta desde último checkpoint)")


class SyncStatus(models.TextChoices):
    PENDING = "PENDING", _("Pendente")
    RUNNING = "RUNNING", _("Em execução")
    COMPLETED = "COMPLETED", _("Concluído")
    FAILED = "FAILED", _("Falhou")


class SyncJob(models.Model):
    """Registro de execução de um sync.

    Job é criado pendente, vira RUNNING ao iniciar, COMPLETED/FAILED ao terminar.
    Permite history em `/admin/sync/` pra Paulo ver "última execução, quantos
    processados, falhou ou não".
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="sync_jobs",
    )
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    capability = models.CharField(max_length=32, choices=Capability.choices)
    mode = models.CharField(max_length=16, choices=SyncMode.choices)
    status = models.CharField(
        max_length=16, choices=SyncStatus.choices, default=SyncStatus.PENDING
    )

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    records_processed = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Job de sync")
        verbose_name_plural = _("Jobs de sync")
        indexes = [
            models.Index(fields=["organization", "source_type", "capability", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.organization.slug} · {self.source_type}/{self.capability} · "
            f"{self.mode} · {self.status}"
        )


class SyncCheckpoint(models.Model):
    """Cursor de incremental por (org, source, capability).

    Atualizado ao final de cada SyncJob bem-sucedido. Usado pra delimitar
    `since` no próximo sync incremental — só pega o que mudou após.
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="sync_checkpoints",
    )
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    capability = models.CharField(max_length=32, choices=Capability.choices)

    last_processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_(
            "Maior data_alteracao/updated_at processada com sucesso. "
            "Próximo incremental usa como 'since'."
        ),
    )
    last_external_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=_(
            "Último external_id processado — backup pra fontes que não têm "
            "updated_at confiável."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Checkpoint de sync")
        verbose_name_plural = _("Checkpoints de sync")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "capability"],
                name="unique_checkpoint_per_source_capability",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"{self.organization.slug} · {self.source_type}/{self.capability} · "
            f"last={self.last_processed_at}"
        )
