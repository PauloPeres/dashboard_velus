"""Models analíticos — dim/fact tables + cache do plano de contas IXC.

Dim tables seguem SCD type 2 (slowly-changing dim com versionamento):
- `current` flag: True = versão atual; False = histórica
- `valid_from`, `valid_to` delimitam o intervalo de validade
- Mudança em campo trackeado fecha versão atual (valid_to=now) e cria nova

Fact tables são append-only quando possível; `fact_contract_status_daily` é
upsert por (org, contract, date) pra suportar re-execução do rebuild.

PlanoContasCache armazena o mapeamento do plano de contas IXC (planejamento +
planejamento_analitico) com TTL, substituindo dicts hardcoded no código.
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

    amount = models.DecimalField(max_digits=12, decimal_places=2)  # principal (valor do boleto)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Multa + juros acumulados (valor_multas + valor_juros do IXC). Separa o
    # principal da operação recorrente do encargo por atraso na inadimplência.
    # O IXC só materializa esse valor no pagamento/reemissão — 0 nas abertas.
    late_fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
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


class FactNetworkSnapshot(TenantModel):
    """Snapshot pontual das métricas de rede — base da série temporal.

    `Connection`/`BandwidthUsage` só guardam o estado atual (sync sobrescreve).
    Esta fact é append-only: a cada captura (Beat) grava 1 linha com a foto
    agregada do momento (contagens por status + banda acumulada), permitindo
    reconstruir tendência de conexões/banda ao longo do tempo em /network/.
    """

    captured_at = models.DateTimeField(db_index=True)
    total_count = models.IntegerField(default=0)
    online_count = models.IntegerField(default=0)
    offline_count = models.IntegerField(default=0)
    blocked_count = models.IntegerField(default=0)
    unknown_count = models.IntegerField(default=0)
    # uptime = online / (online + offline) * 100 — bloqueados não são falha
    uptime_pct = models.DecimalField(max_digits=5, decimal_places=1, default=0)
    rx_bytes_total = models.BigIntegerField(default=0)
    tx_bytes_total = models.BigIntegerField(default=0)

    class Meta:
        verbose_name = _("Fato: snapshot de rede")
        verbose_name_plural = _("Fatos: snapshots de rede")
        indexes = [
            models.Index(fields=["organization", "captured_at"]),
        ]


# =============================================================================
# Churn risk — score de risco de cancelamento por cliente
# =============================================================================
class ChurnRiskScore(TenantModel):
    """Score de risco de churn por cliente — recomputado diariamente.

    Engine baseada em regras (`apps.analytics.application.churn_risk`) avalia
    sinais derivados dos dados já sincronizados (bloqueio prolongado, atraso
    recorrente, chamados frequentes, offline) e grava 1 linha por cliente
    em risco. Clientes sem risco não têm linha (upsert/delete idempotente).

    `signals` é a lista de sinais disparados, cada um:
        {code, label, detail, weight}
    `monthly_amount` = receita líquida em risco (contratos ACTIVE/BLOCKED).
    Puramente analítico — alimenta alertas no dashboard, não dispara ações.
    """

    LEVEL_HIGH = "HIGH"
    LEVEL_MEDIUM = "MEDIUM"
    LEVEL_LOW = "LOW"
    LEVEL_CHOICES = [
        (LEVEL_HIGH, _("Alto")),
        (LEVEL_MEDIUM, _("Médio")),
        (LEVEL_LOW, _("Baixo")),
    ]

    customer = models.ForeignKey(
        "customers.Customer",
        on_delete=models.PROTECT,
        related_name="churn_risk_scores",
    )
    score = models.PositiveSmallIntegerField(default=0)
    level = models.CharField(max_length=8, choices=LEVEL_CHOICES, db_index=True)
    signals = models.JSONField(default=list)
    monthly_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    # Probabilidade de churn (0–1) do modelo ML, quando há modelo treinado pra
    # org e o cliente está ativo. Null = sem modelo / amostra insuficiente.
    ml_probability = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True
    )
    computed_at = models.DateTimeField()

    class Meta:
        verbose_name = _("Score de risco de churn")
        verbose_name_plural = _("Scores de risco de churn")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "customer"],
                name="unique_churn_risk_per_customer",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "-score"]),
            models.Index(fields=["organization", "level"]),
        ]

    def __str__(self) -> str:
        return f"ChurnRisk {self.customer_id} · {self.level} ({self.score})"


class ChurnRiskModel(TenantModel):
    """Modelo de regressão logística treinado por organização (1 corrente).

    Pesos e parâmetros de padronização são persistidos como JSON — o cluster
    k3s tem filesystem efêmero, então nada de pickle em disco. Treinado a
    partir de cancelamentos históricos (`apps.analytics.application.churn_ml`);
    consumido no scoring pra preencher `ChurnRiskScore.ml_probability`.

    `weights` = {feature → coef}, `bias` = intercepto, `feature_means`/
    `feature_stds` = padronização aplicada às features no treino e no score.
    """

    feature_names = models.JSONField(default=list)
    weights = models.JSONField(default=dict)
    bias = models.FloatField(default=0.0)
    feature_means = models.JSONField(default=dict)
    feature_stds = models.JSONField(default=dict)

    n_samples = models.PositiveIntegerField(default=0)
    n_positive = models.PositiveIntegerField(default=0)
    train_accuracy = models.FloatField(default=0.0)
    # Métricas out-of-sample do holdout determinístico (~25%). Null quando o
    # split não tem as duas classes em ambos os lados.
    val_auc = models.FloatField(null=True, blank=True)
    val_accuracy = models.FloatField(null=True, blank=True)

    trained_at = models.DateTimeField()

    class Meta:
        verbose_name = _("Modelo de risco de churn")
        verbose_name_plural = _("Modelos de risco de churn")
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                name="unique_churn_model_per_org",
            ),
        ]

    def __str__(self) -> str:
        return f"ChurnModel {self.organization_id} · n={self.n_samples}"


# =============================================================================
# Plano de Contas IXC — cache sincronizado via `sync_planejamento`
# =============================================================================
class PlanoContasCache(models.Model):
    """Cache do plano de contas IXC para uma organização.

    Armazena dois mapas como JSONField:
    - plano_map: {id_planejamento → {cod, nome, tipo}}  (≈ 91 entradas)
    - conta_map: {id_conta → id_planejamento}           (planejamento_analitico, ≈ 11k entradas)

    Atualizado via `python manage.py sync_planejamento <org_slug>`.
    O aggregation layer lê daqui em vez de dicts hardcoded.
    """

    organization = models.OneToOneField(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="plano_contas_cache",
        verbose_name=_("Organização"),
    )
    # {id_planejamento (str) → {cod, nome, tipo}}
    plano_map = models.JSONField(
        default=dict,
        verbose_name=_("Mapa de planejamento"),
        help_text="id_planejamento → {cod, nome, tipo} — da tabela `planejamento` do IXC",
    )
    # {id_conta (str) → id_planejamento (str)}
    conta_map = models.JSONField(
        default=dict,
        verbose_name=_("Mapa de contas analíticas"),
        help_text="planejamento_analitico.id → id_planejamento — da tabela `planejamento_analitico` do IXC",
    )
    synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Última sincronização"),
    )

    class Meta:
        verbose_name = _("Cache do plano de contas IXC")
        verbose_name_plural = _("Caches do plano de contas IXC")


# =============================================================================
# Fornecedores IXC — cache sincronizado via `sync_fornecedores`
# =============================================================================
class FornecedorCache(models.Model):
    """Cache de fornecedores IXC para uma organização.

    Armazena `supplier_map` como JSONField: {id_fornecedor (str) → nome}.
    Alimentado pelo endpoint `fornecedor` do IXC (fantasia → razao social).

    Resolve nomes no DRE-Contas em tempo de exibição, inclusive para despesas
    gravadas com o fallback antigo `Fornecedor #X` em `Expense.supplier_name`.

    Atualizado via `python manage.py sync_fornecedores <org_slug>` e pelo Beat
    diário que sincroniza o plano de contas (mesma cadência/credenciais IXC).
    """

    organization = models.OneToOneField(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="fornecedor_cache",
        verbose_name=_("Organização"),
    )
    # {id_fornecedor (str) → nome}
    supplier_map = models.JSONField(
        default=dict,
        verbose_name=_("Mapa de fornecedores"),
        help_text="id_fornecedor → nome — da tabela `fornecedor` do IXC",
    )
    synced_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Última sincronização"),
    )

    class Meta:
        verbose_name = _("Cache de fornecedores IXC")
        verbose_name_plural = _("Caches de fornecedores IXC")

    def __str__(self) -> str:
        return f"FornecedorCache {self.organization_id} · n={len(self.supplier_map)}"
