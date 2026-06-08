"""Models de persistencia do bounded context Atendimento.

Herda `apps.shared.TenantModel` -> ganha `organization` FK indexada + TenantManager.
Identidade composta: `(organization, source_type, external_id)` e unique.

Atendimento liga-se ao Customer por **documento (CPF/CNPJ)**, resolvido no
Repository, pois o id de cliente da fonte (Opa) nao bate com o external_id do
Customer (IXC). Por isso guardamos tanto a FK (quando resolvida) quanto o
`customer_document` snapshot.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Departamento(TenantModel):
    """Setor/departamento de atendimento (Comercial, Suporte, Triagem, ...)."""

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do setor no sistema externo (opaco — string)."),
    )
    nome = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=32, blank=True, default="")

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Departamento de atendimento")
        verbose_name_plural = _("Departamentos de atendimento")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_departamento_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "source_type"]),
        ]

    def __str__(self) -> str:
        return f"{self.nome} ({self.source_type}:{self.external_id})"


class Atendimento(TenantModel):
    """Atendimento/conversa omnichannel vindo de uma fonte externa (Opa! Suite, ...).

    `customer` e FK opcional pq o vinculo depende de o documento da conversa
    casar com um Customer ja sincronizado. Repository resolve via
    `(organization, document)` no upsert; `customer_document` guarda o snapshot.
    """

    class Status(models.TextChoices):
        OPEN = "OPEN", _("Aberto")
        IN_PROGRESS = "IN_PROGRESS", _("Em atendimento")
        CLOSED = "CLOSED", _("Finalizado")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do atendimento no sistema externo (opaco — string)."),
    )

    # Vinculo logico via documento (CPF/CNPJ), resolvido no Repository.
    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="atendimentos",
        null=True,
        blank=True,
    )
    customer_external_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True,
        help_text=_("ID opaco do cliente na fonte (nao bate com Customer.external_id)."),
    )
    customer_document = models.CharField(
        max_length=14, blank=True, default="", db_index=True,
        help_text=_("CPF/CNPJ normalizado — ponte logica pro Customer."),
    )
    customer_name = models.CharField(max_length=255, blank=True, default="")

    departamento = models.ForeignKey(
        "atendimento.Departamento",
        on_delete=models.PROTECT,
        related_name="atendimentos",
        null=True,
        blank=True,
    )
    departamento_external_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )

    atendente_external_id = models.CharField(max_length=128, blank=True, default="")
    atendente_nome = models.CharField(max_length=255, blank=True, default="")

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )
    canal = models.CharField(max_length=64, blank=True, default="")
    protocol = models.CharField(max_length=128, blank=True, default="")

    motivos = models.JSONField(default=list, blank=True)
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text=_("Nota humana likert 1-5 (so vem em GET populado)."),
    )

    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Atendimento")
        verbose_name_plural = _("Atendimentos")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "opened_at"]),
            models.Index(fields=["organization", "departamento_external_id"]),
            models.Index(fields=["organization", "customer_document"]),
        ]

    def __str__(self) -> str:
        return f"#{self.protocol} ({self.source_type}:{self.external_id})"


class Mensagem(TenantModel):
    """Mensagem trocada dentro de um Atendimento.

    Ingestao opcional/lazy (1 chamada por atendimento na fonte). FK pro
    atendimento resolvida via `(organization, source_type, atendimento_external_id)`.
    """

    class Direction(models.TextChoices):
        CLIENT = "CLIENT", _("Cliente")
        AGENT = "AGENT", _("Atendente")
        SYSTEM = "SYSTEM", _("Sistema")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID da mensagem no sistema externo (opaco — string)."),
    )

    atendimento = models.ForeignKey(
        "atendimento.Atendimento",
        on_delete=models.CASCADE,
        related_name="mensagens",
        null=True,
        blank=True,
    )
    atendimento_external_id = models.CharField(max_length=128, db_index=True)

    direction = models.CharField(
        max_length=16, choices=Direction.choices, default=Direction.UNKNOWN
    )
    tipo = models.CharField(max_length=64, blank=True, default="")
    texto = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Mensagem de atendimento")
        verbose_name_plural = _("Mensagens de atendimento")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_atendimento_mensagem_per_source",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "source_type", "atendimento_external_id"]
            ),
            models.Index(fields=["organization", "sent_at"]),
        ]

    def __str__(self) -> str:
        return f"msg {self.source_type}:{self.external_id} ({self.direction})"
