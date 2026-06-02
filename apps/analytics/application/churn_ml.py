"""Scoring ML de churn — regressão logística pura-Python (v3).

Treina, por organização, um classificador binário a partir de cancelamentos
históricos e usa a probabilidade prevista pra enriquecer `ChurnRiskScore`.

Por que pura-Python (sem scikit/numpy):
    o cluster k3s é enxuto e o filesystem é efêmero — manter o deploy leve e
    persistir os pesos como JSON no banco (`ChurnRiskModel`) evita dependência
    pesada e pickle em disco que sumiria a cada restart de pod.

Label:
    cliente "churnou" = tem ≥ 1 contrato CANCELED e nenhum ACTIVE/BLOCKED.

Features point-in-time (v3 — corrige o vazamento temporal do #15):
    Cada cliente é fotografado numa **data de referência**:
        positivos (churned) → data do cancelamento (max canceled_at);
        negativos (ativos)  → agora.
    Todas as features usam apenas dados observáveis ATÉ essa data — nada que só
    existiria depois do ponto de observação:
        tenure_days · mrr · n_contracts · late_payments · tickets_total ·
        pay_delay_baseline · pay_delay_recent_dev
    As duas últimas (v4) medem inadimplência *relativa ao próprio cliente*:
    quanto atrasar faz parte do perfil dele (mediana histórica) e o quanto ele
    está atrasando além do normal nos últimos 90 dias. Assim "sempre atrasa e
    segue igual" não é punido — só o desvio do próprio padrão sinaliza risco.
    Assim os churned não têm faturas/chamados posteriores ao cancelamento
    "vazando" pro vetor, e os ativos refletem o estado de hoje — exatamente o
    estado em que serão pontuados em produção. A assimetria de calendário que
    resta (churned observados no passado, ativos no presente) é inerente ao
    snapshot e não constitui leakage.

Validação:
    além da acurácia in-sample, o treino estima generalização num holdout
    determinístico (~25% por id do cliente) e grava AUC e acurácia
    out-of-sample em `ChurnRiskModel`. Uma validação plenamente temporal
    (cortes rolantes) segue como evolução futura.

Complementar às regras: as regras seguem como score primário. Com amostra
insuficiente o treino é pulado e nenhum `ml_probability` é gravado (fallback
para regras).
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import structlog
from django.utils import timezone

from apps.analytics.infrastructure.models import ChurnRiskModel
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

# Ordem estável das features — usada no treino e no score.
FEATURES = (
    "tenure_days",
    "mrr",
    "n_contracts",
    "late_payments",
    "tickets_total",
    "pay_delay_baseline",
    "pay_delay_recent_dev",
)

# ── Limiares de viabilidade do treino ───────────────────────────────────
MIN_SAMPLES = 50
MIN_POSITIVE = 10

# ── Hiperparâmetros do gradiente ────────────────────────────────────────
LEARNING_RATE = 0.1
ITERATIONS = 500
L2_LAMBDA = 0.001

# ── Validação ───────────────────────────────────────────────────────────
# Holdout determinístico: clientes com id ≡ 0 (mod VAL_MODULO) viram validação.
VAL_MODULO = 4


def _sigmoid(z: float) -> float:
    if z < -35:
        return 0.0
    if z > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


# Janela "recente" do comportamento de pagamento, em dias.
RECENT_PAYMENT_WINDOW_DAYS = 90


def _payment_profile(
    invoices: list[tuple[Any, Any]], r: Any, r_date: Any
) -> tuple[float, float]:
    """Perfil de inadimplência *relativo ao próprio cliente*, point-in-time.

    Devolve (baseline, recent_dev):
        baseline    — mediana do atraso (dias) das faturas já liquidadas até `r`.
                      Captura o quanto atrasar "faz parte do perfil" do cliente.
        recent_dev  — atraso médio das faturas vencidas nos últimos
                      RECENT_PAYMENT_WINDOW_DAYS menos o baseline. Positivo =
                      cliente atrasando *mais que o normal dele* → sinal forte;
                      cliente que sempre atrasa e segue igual → ~0 (não-sinal).

    Só usa faturas observáveis até `r` (vencidas até `r_date`); para liquidadas,
    exige `paid <= r`. Sem vazamento temporal.
    """
    settled_lates: list[float] = []
    recent_lates: list[float] = []
    recent_cutoff = r_date - timedelta(days=RECENT_PAYMENT_WINDOW_DAYS)

    for due, paid in invoices:
        if due is None or due > r_date:
            continue  # ainda não vencida na data de referência
        if paid is not None and paid <= r:
            late_days = max((paid.date() - due).days, 0)
            settled_lates.append(float(late_days))
            observed = float(late_days)
        else:
            # Em aberto na data de referência: atraso acumulado até `r`.
            observed = float(max((r_date - due).days, 0))
        if due >= recent_cutoff:
            recent_lates.append(observed)

    baseline = _median(settled_lates)
    if recent_lates:
        recent_avg = sum(recent_lates) / len(recent_lates)
        recent_dev = recent_avg - baseline
    else:
        recent_dev = 0.0
    return baseline, recent_dev


@allow_cross_tenant(reason="treino de churn ML roda em Celery, escopo é a org passada")
def compute_features(
    organization: Organization,
) -> tuple[dict[int, dict[str, float]], set[int], set[int]]:
    """Computa o vetor de features point-in-time por cliente da org.

    Cada cliente é fotografado numa data de referência: cancelamento (churned)
    ou agora (ativos). Só dados observáveis até essa data entram no vetor.

    Retorna (features, churned, active):
        features — {customer_id → {feature → valor}}
        churned  — clientes com label positivo (cancelaram)
        active   — clientes com contrato ACTIVE/BLOCKED (alvos do score)
    """
    from apps.customers.infrastructure.models import Contract
    from apps.financial.infrastructure.models import Invoice
    from apps.helpdesk.infrastructure.models import Ticket

    now = timezone.now()

    contracts_by_cid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    has_live: set[int] = set()
    has_canceled: set[int] = set()
    last_canceled: dict[int, Any] = {}

    for c in Contract.objects.filter(organization=organization).values(
        "customer_id", "status", "monthly_amount", "monthly_amount_addons",
        "monthly_amount_discounts", "activated_at", "canceled_at",
    ):
        cid = c["customer_id"]
        if cid is None:
            continue
        contracts_by_cid[cid].append(c)
        if c["status"] in ("ACTIVE", "BLOCKED"):
            has_live.add(cid)
        if c["status"] == "CANCELED":
            has_canceled.add(cid)
            can = c["canceled_at"]
            if can is not None and (cid not in last_canceled or can > last_canceled[cid]):
                last_canceled[cid] = can

    churned = has_canceled - has_live
    active = set(has_live)

    # Data de referência por cliente: cancelamento (churned) ou agora (ativos).
    ref: dict[int, Any] = {
        cid: (last_canceled[cid] if (cid in churned and cid in last_canceled) else now)
        for cid in contracts_by_cid
    }

    # Faturas por cliente: (due_date, paid_at) — pra contar atraso point-in-time.
    invoices_by_cid: dict[int, list[tuple[Any, Any]]] = defaultdict(list)
    for row in Invoice.objects.filter(organization=organization).values(
        "contract__customer_id", "due_date", "paid_at"
    ):
        cid = row["contract__customer_id"]
        if cid is None:
            continue
        invoices_by_cid[cid].append((row["due_date"], row["paid_at"]))

    # Chamados por cliente: lista de opened_at.
    tickets_by_cid: dict[int, list[Any]] = defaultdict(list)
    for row in Ticket.objects.filter(organization=organization).values(
        "customer_id", "opened_at"
    ):
        cid = row["customer_id"]
        if cid is None:
            continue
        tickets_by_cid[cid].append(row["opened_at"])

    features: dict[int, dict[str, float]] = {}
    for cid, clist in contracts_by_cid.items():
        r = ref[cid]
        r_date = r.date()

        # Contratos que já existiam na data de referência.
        existed = [
            c for c in clist
            if c["activated_at"] is None or c["activated_at"] <= r
        ]
        n_contracts = len(existed) if existed else len(clist)

        # Tenure: do primeiro contrato ativado até a data de referência.
        starts = [c["activated_at"] for c in existed if c["activated_at"] is not None]
        tenure_days = max((r - min(starts)).days, 0) if starts else 0

        # MRR: soma líquida dos contratos vivos na data de referência.
        mrr_val = Decimal("0")
        for c in clist:
            act = c["activated_at"]
            can = c["canceled_at"]
            if act is not None and act > r:
                continue
            if can is not None and can < r:
                continue
            mrr_val += (
                (c["monthly_amount"] or Decimal("0"))
                + (c["monthly_amount_addons"] or Decimal("0"))
                - (c["monthly_amount_discounts"] or Decimal("0"))
            )

        # Atraso point-in-time: faturas vencidas até `r` e ainda não pagas em `r`.
        late = sum(
            1
            for due, paid in invoices_by_cid.get(cid, ())
            if due is not None and due <= r_date and (paid is None or paid > r)
        )

        # Chamados abertos até a data de referência.
        tickets_total = sum(
            1 for opened in tickets_by_cid.get(cid, ()) if opened is not None and opened <= r
        )

        # Inadimplência relativa ao próprio perfil do cliente (point-in-time).
        pay_baseline, pay_recent_dev = _payment_profile(
            invoices_by_cid.get(cid, ()), r, r_date
        )

        features[cid] = {
            "tenure_days": float(tenure_days),
            "mrr": float(mrr_val),
            "n_contracts": float(n_contracts),
            "late_payments": float(late),
            "tickets_total": float(tickets_total),
            "pay_delay_baseline": pay_baseline,
            "pay_delay_recent_dev": pay_recent_dev,
        }

    return features, churned, active


def _standardize(
    rows: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    """Padroniza colunas (z-score). Retorna (matriz, means, stds)."""
    n = len(rows)
    f = len(FEATURES)
    means = [0.0] * f
    stds = [0.0] * f
    for j in range(f):
        col = [r[j] for r in rows]
        m = sum(col) / n
        var = sum((x - m) ** 2 for x in col) / n
        means[j] = m
        stds[j] = math.sqrt(var) or 1.0
    std_rows = [
        [(r[j] - means[j]) / stds[j] for j in range(f)] for r in rows
    ]
    return std_rows, means, stds


def _apply_standardize(
    rows: list[list[float]], means: list[float], stds: list[float]
) -> list[list[float]]:
    """Aplica padronização pré-computada (means/stds do treino) a novas linhas."""
    f = len(FEATURES)
    return [
        [(r[j] - means[j]) / (stds[j] or 1.0) for j in range(f)] for r in rows
    ]


def _train_weights(
    std_rows: list[list[float]], labels: list[float]
) -> tuple[list[float], float]:
    """Gradiente descendente da regressão logística com regularização L2."""
    f = len(FEATURES)
    n = len(std_rows)
    w = [0.0] * f
    b = 0.0
    for _ in range(ITERATIONS):
        grad_w = [0.0] * f
        grad_b = 0.0
        for i, x in enumerate(std_rows):
            z = b + sum(w[j] * x[j] for j in range(f))
            err = _sigmoid(z) - labels[i]
            for j in range(f):
                grad_w[j] += err * x[j]
            grad_b += err
        for j in range(f):
            grad_w[j] = grad_w[j] / n + L2_LAMBDA * w[j]
            w[j] -= LEARNING_RATE * grad_w[j]
        b -= LEARNING_RATE * (grad_b / n)
    return w, b


def _predict_std(
    std_rows: list[list[float]], w: list[float], b: float
) -> list[float]:
    f = len(FEATURES)
    return [_sigmoid(b + sum(w[j] * x[j] for j in range(f))) for x in std_rows]


def _accuracy(preds: list[float], labels: list[float]) -> float:
    correct = sum(
        1 for p, lbl in zip(preds, labels, strict=True) if (1.0 if p >= 0.5 else 0.0) == lbl
    )
    return correct / len(labels)


def _auc(scores: list[float], labels: list[float]) -> float | None:
    """AUC ROC via estatística de Mann-Whitney (média de ranks com empates).

    Retorna None quando uma das classes está ausente (AUC indefinida).
    """
    n_pos = sum(1 for lbl in labels if lbl == 1.0)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None

    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # ranks 1-based, média nos empates
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    sum_pos = sum(ranks[idx] for idx in range(len(labels)) if labels[idx] == 1.0)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _holdout_validation(
    rows: list[list[float]], labels: list[float], cids: list[int]
) -> tuple[float | None, float | None]:
    """Estima generalização num holdout determinístico (~25% por id).

    Treina nas linhas de treino, padroniza a validação com as estatísticas do
    treino (sem leakage de padronização) e retorna (auc, acurácia) out-of-sample.
    Retorna (None, None) quando o split não tem as duas classes em ambos os lados.
    """
    train_idx = [i for i, cid in enumerate(cids) if cid % VAL_MODULO != 0]
    val_idx = [i for i, cid in enumerate(cids) if cid % VAL_MODULO == 0]
    if not train_idx or not val_idx:
        return None, None

    tr_labels = [labels[i] for i in train_idx]
    va_labels = [labels[i] for i in val_idx]
    if len(set(tr_labels)) < 2 or len(set(va_labels)) < 2:
        return None, None

    tr_rows = [rows[i] for i in train_idx]
    va_rows = [rows[i] for i in val_idx]
    std_tr, means, stds = _standardize(tr_rows)
    w, b = _train_weights(std_tr, tr_labels)
    preds = _predict_std(_apply_standardize(va_rows, means, stds), w, b)
    return _auc(preds, va_labels), _accuracy(preds, va_labels)


@allow_cross_tenant(reason="treino de churn ML roda em Celery, escopo é a org passada")
def train_churn_model(organization: Organization) -> dict[str, Any] | None:
    """Treina e persiste o modelo de churn da org. Retorna resumo ou None
    quando a amostra é insuficiente (fallback para regras)."""
    features, churned, _active = compute_features(organization)

    cids = list(features)
    n_samples = len(cids)
    n_positive = sum(1 for cid in cids if cid in churned)

    if n_samples < MIN_SAMPLES or n_positive < MIN_POSITIVE:
        _logger.info(
            "churn_ml_skip_insufficient",
            org=organization.slug,
            n_samples=n_samples,
            n_positive=n_positive,
        )
        return None

    rows = [[features[cid][name] for name in FEATURES] for cid in cids]
    labels = [1.0 if cid in churned else 0.0 for cid in cids]

    # Estima generalização antes de treinar o modelo final em toda a amostra.
    val_auc, val_accuracy = _holdout_validation(rows, labels, cids)

    # Modelo final usa toda a amostra (dado escasso — aproveita todo o sinal).
    std_rows, means, stds = _standardize(rows)
    w, b = _train_weights(std_rows, labels)
    accuracy = _accuracy(_predict_std(std_rows, w, b), labels)

    f = len(FEATURES)
    now = timezone.now()
    ChurnRiskModel.objects.update_or_create(
        organization=organization,
        defaults={
            "feature_names": list(FEATURES),
            "weights": {FEATURES[j]: w[j] for j in range(f)},
            "bias": b,
            "feature_means": {FEATURES[j]: means[j] for j in range(f)},
            "feature_stds": {FEATURES[j]: stds[j] for j in range(f)},
            "n_samples": n_samples,
            "n_positive": n_positive,
            "train_accuracy": accuracy,
            "val_auc": val_auc,
            "val_accuracy": val_accuracy,
            "trained_at": now,
        },
    )
    summary = {
        "n_samples": n_samples,
        "n_positive": n_positive,
        "accuracy": round(accuracy, 4),
        "val_auc": round(val_auc, 4) if val_auc is not None else None,
        "val_accuracy": round(val_accuracy, 4) if val_accuracy is not None else None,
    }
    _logger.info("churn_ml_trained", org=organization.slug, **summary)
    return summary


def predict_probabilities(
    model: ChurnRiskModel, features: dict[int, dict[str, float]]
) -> dict[int, float]:
    """Aplica o modelo persistido aos vetores de features → {cid → prob}."""
    names = list(model.feature_names)
    means = model.feature_means
    stds = model.feature_stds
    weights = model.weights
    out: dict[int, float] = {}
    for cid, vec in features.items():
        z = model.bias
        for name in names:
            std = stds.get(name) or 1.0
            x = (vec.get(name, 0.0) - means.get(name, 0.0)) / std
            z += weights.get(name, 0.0) * x
        out[cid] = _sigmoid(z)
    return out


@allow_cross_tenant(reason="lookup do modelo de churn por org, fora de request HTTP")
def get_current_model(organization: Organization) -> ChurnRiskModel | None:
    return ChurnRiskModel.objects.filter(organization=organization).first()
