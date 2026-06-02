"""Engine de predição de churn — scoring baseado em regras (v1).

Avalia, por cliente, sinais de risco de cancelamento derivados dos dados já
sincronizados (sem ML, sem fontes novas) e persiste em `ChurnRiskScore`:

    1. Contrato bloqueado há ≥ 30 dias consecutivos        peso 40
    2. Atraso recorrente (≥ 3 faturas vencidas em 6 meses)  peso 25
    3. Chamados frequentes (≥ 3 nos últimos 30 dias)        peso 20
    4. Offline com contrato ativo                           peso 15

Score = soma dos pesos disparados (capado em 100). Nível:
    HIGH ≥ 50 · MEDIUM ≥ 25 · LOW > 0

Idempotente: clientes em risco têm 1 linha upsertada; clientes que saíram do
risco têm a linha removida. Puramente analítico — alimenta alertas no
dashboard, nenhuma ação é disparada.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import structlog
from django.db.models import Count, Max
from django.utils import timezone

from apps.analytics.infrastructure.models import (
    ChurnRiskScore,
    FactContractStatusDaily,
)
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

_ZERO = Decimal("0.00")

# ── Pesos dos sinais (v1) — soma capada em 100 ──────────────────────────
W_BLOCKED = 40
W_LATE_PAYMENTS = 25
W_FREQUENT_TICKETS = 20
W_OFFLINE = 15

# ── Limiares ────────────────────────────────────────────────────────────
BLOCKED_MIN_DAYS = 30
LATE_PAYMENTS_WINDOW_DAYS = 180  # ~6 meses
LATE_PAYMENTS_MIN = 3
TICKETS_WINDOW_DAYS = 30
TICKETS_MIN = 3

LEVEL_HIGH_MIN = 50
LEVEL_MEDIUM_MIN = 25


def _level_for(score: int) -> str:
    if score >= LEVEL_HIGH_MIN:
        return ChurnRiskScore.LEVEL_HIGH
    if score >= LEVEL_MEDIUM_MIN:
        return ChurnRiskScore.LEVEL_MEDIUM
    return ChurnRiskScore.LEVEL_LOW


@allow_cross_tenant(reason="engine de churn risk roda em Celery, escopo é a org passada")
def compute_churn_risk_scores(organization: Organization) -> dict[str, Any]:
    """Recomputa os scores de risco de churn da org e persiste em ChurnRiskScore.

    Retorna um resumo com contagens por nível e quantos registros obsoletos
    foram removidos.
    """
    from apps.customers.infrastructure.models import Contract
    from apps.financial.infrastructure.models import Invoice
    from apps.helpdesk.infrastructure.models import Ticket
    from apps.network.infrastructure.models import Connection

    today = timezone.now().date()
    now = timezone.now()

    signals: dict[int, list[dict[str, Any]]] = defaultdict(list)
    mrr: dict[int, Decimal] = defaultdict(lambda: _ZERO)
    active_customers: set[int] = set()
    blocked_contract_customer: dict[int, int] = {}

    # ── Receita em risco + população relevante ──────────────────────────
    # Só contratos ACTIVE/BLOCKED geram MRR — base pra "receita em risco".
    for c in Contract.objects.filter(
        organization=organization, status__in=("ACTIVE", "BLOCKED")
    ):
        mrr[c.customer_id] += c.monthly_amount_net
        if c.status == "ACTIVE":
            active_customers.add(c.customer_id)
        elif c.status == "BLOCKED":
            blocked_contract_customer[c.id] = c.customer_id

    # ── Sinal 1: contrato bloqueado há ≥ 30 dias ────────────────────────
    _apply_blocked_signal(
        organization, today, blocked_contract_customer, signals
    )

    # ── Sinal 2: atraso recorrente (≥ 3 faturas vencidas em 6 meses) ────
    pay_cutoff = today - timedelta(days=LATE_PAYMENTS_WINDOW_DAYS)
    late_rows = (
        Invoice.objects.filter(
            organization=organization,
            status__in=("PENDING", "OVERDUE"),
            due_date__gte=pay_cutoff,
            due_date__lt=today,
        )
        .values("contract__customer_id")
        .annotate(n=Count("id"))
        .filter(n__gte=LATE_PAYMENTS_MIN)
    )
    for row in late_rows:
        cid = row["contract__customer_id"]
        if cid is None:
            continue
        signals[cid].append({
            "code": "LATE_PAYMENTS",
            "label": "Atraso recorrente",
            "detail": f"{row['n']} faturas vencidas nos últimos 6 meses",
            "weight": W_LATE_PAYMENTS,
        })

    # ── Sinal 3: chamados frequentes (≥ 3 nos últimos 30 dias) ──────────
    ticket_cutoff = now - timedelta(days=TICKETS_WINDOW_DAYS)
    ticket_rows = (
        Ticket.objects.filter(
            organization=organization, opened_at__gte=ticket_cutoff
        )
        .values("customer_id")
        .annotate(n=Count("id"))
        .filter(n__gte=TICKETS_MIN)
    )
    for row in ticket_rows:
        cid = row["customer_id"]
        if cid is None:
            continue
        signals[cid].append({
            "code": "FREQUENT_TICKETS",
            "label": "Chamados frequentes",
            "detail": f"{row['n']} chamados nos últimos 30 dias",
            "weight": W_FREQUENT_TICKETS,
        })

    # ── Sinal 4: offline com contrato ativo ─────────────────────────────
    if active_customers:
        offline_ids = set(
            Connection.objects.filter(
                organization=organization,
                status=Connection.Status.OFFLINE,
                customer_id__in=active_customers,
            ).values_list("customer_id", flat=True)
        )
        for cid in offline_ids:
            signals[cid].append({
                "code": "OFFLINE",
                "label": "Offline com contrato ativo",
                "detail": "Conexão offline apesar de contrato ativo",
                "weight": W_OFFLINE,
            })

    # ── Persistência idempotente ────────────────────────────────────────
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for cid, sig_list in signals.items():
        score = min(100, sum(s["weight"] for s in sig_list))
        level = _level_for(score)
        counts[level] += 1
        clean = [
            {k: v for k, v in s.items() if not k.startswith("_")}
            for s in sorted(sig_list, key=lambda s: s["weight"], reverse=True)
        ]
        ChurnRiskScore.objects.update_or_create(
            organization=organization,
            customer_id=cid,
            defaults={
                "score": score,
                "level": level,
                "signals": clean,
                "monthly_amount": mrr[cid],
                "computed_at": now,
            },
        )

    deleted, _ = (
        ChurnRiskScore.objects.filter(organization=organization)
        .exclude(customer_id__in=signals.keys())
        .delete()
    )

    summary = {
        "at_risk": len(signals),
        "high": counts["HIGH"],
        "medium": counts["MEDIUM"],
        "low": counts["LOW"],
        "deleted": deleted,
    }
    _logger.info("churn_risk_computed", org=organization.slug, **summary)
    return summary


def _apply_blocked_signal(
    organization: Organization,
    today: Any,
    blocked_contract_customer: dict[int, int],
    signals: dict[int, list[dict[str, Any]]],
) -> None:
    """Dispara o sinal de bloqueio prolongado a partir do FactContractStatusDaily.

    Dias bloqueados = (hoje - último snapshot não-bloqueado) - 1. Sem snapshot
    não-bloqueado anterior, assume 999 (bloqueado desde o início da série) —
    mesma heurística de `compute_at_risk_contracts`.
    """
    if not blocked_contract_customer:
        return

    blocked_ids = list(blocked_contract_customer.keys())
    last_non_blocked = {
        row["contract_id"]: row["last_date"]
        for row in FactContractStatusDaily.objects.filter(
            organization=organization,
            contract_id__in=blocked_ids,
            date__lt=today,
        )
        .exclude(status="BLOCKED")
        .values("contract_id")
        .annotate(last_date=Max("date"))
    }

    for cid_contract, customer_id in blocked_contract_customer.items():
        last_ok = last_non_blocked.get(cid_contract)
        days = (today - last_ok).days - 1 if last_ok else 999
        if days < BLOCKED_MIN_DAYS:
            continue
        # Um cliente com múltiplos contratos bloqueados dispara o sinal uma vez,
        # mantendo o maior número de dias.
        existing = next(
            (s for s in signals[customer_id] if s["code"] == "CONTRACT_BLOCKED"),
            None,
        )
        if existing:
            prev = existing.get("_days", 0)
            if days > prev:
                existing["_days"] = days
                existing["detail"] = f"Contrato bloqueado há {days} dias"
            continue
        signals[customer_id].append({
            "code": "CONTRACT_BLOCKED",
            "label": "Bloqueio prolongado",
            "detail": f"Contrato bloqueado há {days} dias",
            "weight": W_BLOCKED,
            "_days": days,
        })
