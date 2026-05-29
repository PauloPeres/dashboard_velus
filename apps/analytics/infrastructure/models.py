"""Models analíticos — dim/fact tables.

Dim tables seguem SCD type 2 (slowly-changing dim com versionamento):
- `current` flag: True = versão atual; False = histórica
- `valid_from`, `valid_to` delimitam o intervalo de validade
- Mudança em campo trackeado fecha versão atual (valid_to=now) e cria nova

Fact tables são append-only quando possível; `fact_contract_status_daily` é
upsert por (org, contract, date) pra suportar re-execução do rebuild.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.integrations.shared.enums import SourceType
from apps.shared.models import TenantModel


# =============================================================================
# Dim tables (SCD type 2)
# =============================================================================
class DimCustomer(TenantModel):
    """Versão histórica de um cliente.

    Chave natural: (org, source_type, external_id). Pode haver múltiplas linhas
    por chave (uma por versão); `current=True` aponta pra atual.
    """

    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    external_id = models.CharField(max_length=128)

    # Atributos rastreados pra SCD2
    name = models.CharField(max_length=255)
    document = models.CharField(max_length=14, db_index=True)
    status = models.CharField(max_length=16)

    # SCD2
    current = models.BooleanField(default=True, db_index=True)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Dim Customer")
        verbose_name_plural = _("Dim Customers")
        indexes = [
            models.Index(fields=["organization", "source_type", "external_id", "current"]),
            models.Index(fields=["organization", "document"]),
        ]

    def __str__(self) -> str:
        return f"DimCustomer {self.name} (v={self.valid_from})"


class DimContract(TenantModel):
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    external_id = models.CharField(max_length=128)

    plan_name = models.CharField(max_length=128)
    monthly_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=24)

    # SCD2
    current = models.BooleanField(default=True, db_index=True)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("Dim Contract")
        verbose_name_plural = _("Dim Contracts")
        indexes = [
            models.Index(fields=["organization", "source_type", "external_id", "current"]),
        ]

    def __str__(self) -> str:
        return f"DimContract {self.external_id} ({self.plan_name})"


class DimPlan(TenantModel):
    """Plano agregado por nome — não é SCD2 (planos viram referência estável).

    Quando plano novo aparece em algum contrato, registra aqui.
    """

    name = models.CharField(max_length=128)
    monthly_amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = _("Dim Plan")
        verbose_name_plural = _("Dim Plans")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_dim_plan_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} (R$ {self.monthly_amount})"


# =============================================================================
# Fact tables
# =============================================================================
class FactContractStatusDaily(TenantModel):
    """Snapshot diário do status de cada contrato — fundação pra MRR/churn/ativos.

    1 linha por (org, contract, date). Permite séries temporais sem janela
    de perda — mesmo se sync atrasar, rebuild reconstrói.
    """

    contract = models.ForeignKey(
        "customers.Contract",
        on_delete=models.PROTECT,
        related_name="status_daily",
    )
    date = models.DateField(db_index=True)
    status = models.CharField(max_length=24)
    monthly_amount = models.DecimalField(max_digits=12, decimal_places=2)
    # is_active = status in (ACTIVE, BLOCKED, AWAITING_INSTALL) — facilita queries
    is_active = models.BooleanField(default=False)

    class Meta:
        verbose_name = _("Fato: contrato/status diário")
        verbose_name_plural = _("Fatos: contratos/status diário")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "contract", "date"],
                name="unique_fact_contract_status_per_day",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "date", "is_active"]),
        ]


class FactInvoice(TenantModel):
    """Evento de fatura/boleto — usado pra inadimplência e aging."""

    invoice = models.ForeignKey(
        "financial.Invoice",
        on_delete=models.PROTECT,
        related_name="fact",
    )
    issued_date = models.DateField(db_index=True)
    due_date = models.DateField(db_index=True)
    paid_date = models.DateField(null=True, blank=True, db_index=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=16)

    # Derivados pra acelerar queries de aging
    days_overdue = models.IntegerField(default=0)  # >0 se em atraso
    aging_bucket = models.CharField(max_length=16, default="ON_TIME")
    # ON_TIME | 0_30 | 31_60 | 61_90 | OVER_90 | PAID | CANCELED

    class Meta:
        verbose_name = _("Fato: fatura")
        verbose_name_plural = _("Fatos: faturas")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "invoice"],
                name="unique_fact_invoice",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "status", "aging_bucket"]),
            models.Index(fields=["organization", "due_date"]),
        ]


class FactPayment(TenantModel):
    """Evento de recebimento — usado pra fluxo de caixa real."""

    payment = models.ForeignKey(
        "financial.Payment",
        on_delete=models.PROTECT,
        related_name="fact",
    )
    paid_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=16)

    class Meta:
        verbose_name = _("Fato: pagamento")
        verbose_name_plural = _("Fatos: pagamentos")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "payment"],
                name="unique_fact_payment",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "paid_date"]),
        ]


class FactExpense(TenantModel):
    """Evento de despesa — usado pra fluxo de caixa real e DRE.

    `expense_date` = paid_at se pago, due_date se em aberto/cancelado.
    Permite séries temporais de saídas de caixa consistentes.
    """

    expense = models.ForeignKey(
        "financial.Expense",
        on_delete=models.PROTECT,
        related_name="fact",
    )
    expense_date = models.DateField(db_index=True)  # paid_at ou due_date
    due_date = models.DateField()
    paid_date = models.DateField(null=True, blank=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    paid_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(max_length=16)
    category = models.CharField(max_length=128, blank=True, default="")
    supplier_name = models.CharField(max_length=255, blank=True, default="")
    description = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        verbose_name = _("Fato: despesa")
        verbose_name_plural = _("Fatos: despesas")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "expense"],
                name="unique_fact_expense",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "expense_date"]),
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["organization", "category"]),
        ]
