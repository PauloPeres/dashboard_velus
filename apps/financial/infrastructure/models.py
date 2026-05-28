"""Models de persistência do Financial.

Cross-context FK: Invoice/Payment referenciam Contract (de apps.customers).
Aceito explicitamente pra não criar lógica de resolução por documento;
Repository resolve FK por (organization, source_type, contract_external_id).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


class Invoice(TenantModel):
    """Fatura/boleto — recebível."""

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pendente")
        PAID = "PAID", _("Paga")
        OVERDUE = "OVERDUE", _("Em atraso")
        CANCELED = "CANCELED", _("Cancelada")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    external_id = models.CharField(max_length=128)

    contract = models.ForeignKey(
        "customers.Contract",
        on_delete=models.PROTECT,
        related_name="invoices",
        null=True,
        blank=True,
    )
    contract_external_id = models.CharField(max_length=128, db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    due_date = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNKNOWN)

    issued_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Fatura")
        verbose_name_plural = _("Faturas")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_invoice_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "due_date"]),
            models.Index(fields=["organization", "contract"]),
            models.Index(fields=["organization", "source_type", "contract_external_id"]),
        ]

    def __str__(self) -> str:
        return f"Fatura {self.source_type}:{self.external_id} R$ {self.amount} ({self.status})"

    @property
    def is_overdue(self) -> bool:
        from django.utils import timezone
        return self.status == self.Status.PENDING and self.due_date < timezone.now().date()


class Payment(TenantModel):
    """Recebimento — entrada de caixa.

    Pode ou não estar atrelado a uma Invoice/Contract específica (alguns
    sistemas registram pagamento avulso). FKs opcionais.
    """

    class Method(models.TextChoices):
        BOLETO = "BOLETO", _("Boleto")
        PIX = "PIX", _("PIX")
        TRANSFER = "TRANSFER", _("Transferência")
        CASH = "CASH", _("Dinheiro")
        CARD = "CARD", _("Cartão")
        UNKNOWN = "UNKNOWN", _("Desconhecido")

    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    external_id = models.CharField(max_length=128)

    invoice = models.ForeignKey(
        "financial.Invoice",
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )
    contract = models.ForeignKey(
        "customers.Contract",
        on_delete=models.PROTECT,
        related_name="payments",
        null=True,
        blank=True,
    )
    invoice_external_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    contract_external_id = models.CharField(max_length=128, blank=True, default="", db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_at = models.DateTimeField(db_index=True)
    method = models.CharField(max_length=16, choices=Method.choices, default=Method.UNKNOWN)

    raw_extras = models.JSONField(default=dict, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Pagamento")
        verbose_name_plural = _("Pagamentos")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "external_id"],
                name="unique_payment_per_source",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "paid_at"]),
            models.Index(fields=["organization", "method"]),
        ]

    def __str__(self) -> str:
        return f"Pagamento {self.source_type}:{self.external_id} R$ {self.amount}"
