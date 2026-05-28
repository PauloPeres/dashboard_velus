"""Models de persistência do bounded context Customers.

Atenção:
- Herda `apps.shared.TenantModel` → ganha `organization` FK indexada + TenantManager.
- Identidade composta: `(organization, source_type, external_id)` é unique.
- `document` (CPF/CNPJ) é a chave de identidade LÓGICA cross-source — quando o
  mesmo cliente físico existe em IXC E em ContaAzul, são 2 linhas com mesmo
  document mas source_type diferente.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Customer(TenantModel):
    """Cliente vindo de uma fonte externa (IXC, ContaAzul, ...).

    `source_type` é o discriminador que permite múltiplas fontes pra mesma org
    contribuirem registros sem colisão. Merge lógico vive em analytics
    (`fact_customer` dedup por `document`).
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", _("Ativo")
        BLOCKED = "BLOCKED", _("Bloqueado")
        CANCELED = "CANCELED", _("Cancelado")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(
        max_length=32,
        choices=SourceType.choices,
        help_text=_("Sistema externo que originou este registro."),
    )
    external_id = models.CharField(
        max_length=128,
        help_text=_("ID do cliente no sistema externo (opaco — string)."),
    )

    # Identidade lógica cross-source
    document = models.CharField(
        max_length=14,
        db_index=True,
        help_text=_("CPF (11 dígitos) ou CNPJ (14 dígitos), só números."),
    )

    name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )

    # Quando o cliente foi criado no sistema externo (não confundir com created_at)
    created_at_source = models.DateTimeField(null=True, blank=True)

    # Campos source-specific opacos. Domain NÃO ACESSA — só persiste pra debug.
    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Cliente")
        verbose_name_plural = _("Clientes")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_customer_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "document"]),
            models.Index(fields=["organization", "source_type"]),
            models.Index(fields=["organization", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.source_type}:{self.external_id})"
