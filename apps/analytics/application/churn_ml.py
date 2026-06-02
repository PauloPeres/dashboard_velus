"""Scoring ML de churn — regressão logística pura-Python (v2).

Treina, por organização, um classificador binário a partir de cancelamentos
históricos e usa a probabilidade prevista pra enriquecer `ChurnRiskScore`.

Por que pura-Python (sem scikit/numpy):
    o cluster k3s é enxuto e o filesystem é efêmero — manter o deploy leve e
    persistir os pesos como JSON no banco (`ChurnRiskModel`) evita dependência
    pesada e pickle em disco que sumiria a cada restart de pod.

Label:
    cliente "churnou" = tem ≥ 1 contrato CANCELED e nenhum ACTIVE/BLOCKED.

Features (acumulam ao longo da vida do cliente, não zeram no cancelamento, o
que mantém algum sinal mesmo em estado pós-churn):
    tenure_days · mrr · n_contracts · late_payments · tickets_total

Limitação conhecida (v2): as features são computadas "como hoje", não
point-in-time no momento do cancelamento. É um modelo complementar às regras,
não substituto — as regras seguem como score primário. Com amostra
insuficiente, o treino é pulado e nenhum `ml_probability` é gravado (fallback
para regras).
"""

from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import Any

import structlog
from django.db.models import Count
from django.utils import timezone

from apps.analytics.infrastructure.models import ChurnRiskModel
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

# Ordem estável das features — usada no treino e no score.
FEATURES = ("tenure_days", "mrr", "n_contracts", "late_payments", "tickets_total")

# ── Limiares de viabilidade do treino ───────────────────────────────────
MIN_SAMPLES = 50
MIN_POSITIVE = 10

# ── Hiperparâmetros do gradiente ────────────────────────────────────────
LEARNING_RATE = 0.1
ITERATIONS = 500
L2_LAMBDA = 0.001


def _sigmoid(z: float) -> float:
    if z < -35:
        return 0.0
    if z > 35:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


@allow_cross_tenant(reason="treino de churn ML roda em Celery, escopo é a org passada")
def compute_features(
    organization: Organization,
) -> tuple[dict[int, dict[str, float]], set[int], set[int]]:
    """Computa o vetor de features por cliente da org.

    Retorna (features, churned, active):
        features — {customer_id → {feature → valor}}
        churned  — clientes com label positivo (cancelaram)
        active   — clientes com contrato ACTIVE/BLOCKED (alvos do score)
    """
    from apps.customers.infrastructure.models import Contract
    from apps.financial.infrastructure.models import Invoice
    from apps.helpdesk.infrastructure.models import Ticket

    now = timezone.now()

    n_contracts: dict[int, int] = defaultdict(int)
    mrr: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    earliest: dict[int, Any] = {}
    last_canceled: dict[int, Any] = {}
    has_live: set[int] = set()
    has_canceled: set[int] = set()

    for c in Contract.objects.filter(organization=organization).values(
        "customer_id", "status", "monthly_amount", "monthly_amount_addons",
        "monthly_amount_discounts", "activated_at", "canceled_at",
    ):
        cid = c["customer_id"]
        if cid is None:
            continue
        n_contracts[cid] += 1
        net = (
            (c["monthly_amount"] or Decimal("0"))
            + (c["monthly_amount_addons"] or Decimal("0"))
            - (c["monthly_amount_discounts"] or Decimal("0"))
        )
        mrr[cid] += net
        act = c["activated_at"]
        if act is not None and (cid not in earliest or act < earliest[cid]):
            earliest[cid] = act
        if c["status"] in ("ACTIVE", "BLOCKED"):
            has_live.add(cid)
        if c["status"] == "CANCELED":
            has_canceled.add(cid)
            can = c["canceled_at"]
            if can is not None and (cid not in last_canceled or can > last_canceled[cid]):
                last_canceled[cid] = can

    late_counts = {
        row["contract__customer_id"]: row["n"]
        for row in Invoice.objects.filter(
            organization=organization, status__in=("PENDING", "OVERDUE")
        )
        .values("contract__customer_id")
        .annotate(n=Count("id"))
        if row["contract__customer_id"] is not None
    }
    ticket_counts = {
        row["customer_id"]: row["n"]
        for row in Ticket.objects.filter(organization=organization)
        .values("customer_id")
        .annotate(n=Count("id"))
        if row["customer_id"] is not None
    }

    churned = has_canceled - has_live
    active = set(has_live)

    features: dict[int, dict[str, float]] = {}
    for cid in n_contracts:
        start = earliest.get(cid)
        end = last_canceled.get(cid) if cid in churned else now
        if start is not None and end is not None and end > start:
            tenure_days = (end - start).days
        else:
            tenure_days = 0
        features[cid] = {
            "tenure_days": float(tenure_days),
            "mrr": float(mrr[cid]),
            "n_contracts": float(n_contracts[cid]),
            "late_payments": float(late_counts.get(cid, 0)),
            "tickets_total": float(ticket_counts.get(cid, 0)),
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
    std_rows, means, stds = _standardize(rows)

    f = len(FEATURES)
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
            grad_w[j] = grad_w[j] / n_samples + L2_LAMBDA * w[j]
            w[j] -= LEARNING_RATE * grad_w[j]
        b -= LEARNING_RATE * (grad_b / n_samples)

    correct = 0
    for i, x in enumerate(std_rows):
        z = b + sum(w[j] * x[j] for j in range(f))
        pred = 1.0 if _sigmoid(z) >= 0.5 else 0.0
        if pred == labels[i]:
            correct += 1
    accuracy = correct / n_samples

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
            "trained_at": now,
        },
    )
    summary = {
        "n_samples": n_samples,
        "n_positive": n_positive,
        "accuracy": round(accuracy, 4),
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
