"""Engine de predição de churn — scoring baseado em regras (v1).

Avalia, por cliente, sinais de risco de cancelamento derivados dos dados já
sincronizados (sem ML, sem fontes novas) e persiste em `ChurnRiskScore`:

    1. Contrato bloqueado há ≥ 30 dias consecutivos        peso 40
    2. Atraso recorrente (≥ 3 faturas vencidas em 6 meses)  peso 25
    3. Chamados de suporte frequentes (≥ 3 em 30d, só tipos
       de problema técnico/rede/suporte — exclui rotina)    peso 20
    4. Downgrade de plano (valor mensal caiu vs anterior)   peso 20
    5. Offline com contrato ativo                           peso 15
    6. Queda brusca de consumo de banda (≥ 70% em 30d)      peso 15
    7. Insatisfação nas conversas (léxico PT-BR × likert)   peso 15

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
from apps.helpdesk.application.os_classification import churn_relevant_subject_ids
from apps.helpdesk.application.os_lookups import load_os_lookups
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

_ZERO = Decimal("0.00")

# ── Pesos dos sinais (v1) — soma capada em 100 ──────────────────────────
W_BLOCKED = 40
W_LATE_PAYMENTS = 25
W_FREQUENT_TICKETS = 20
W_DOWNGRADE = 20
W_OFFLINE = 15
W_BANDWIDTH_DROP = 15
W_DISSATISFACTION = 15  # tom negativo nas conversas + nota likert baixa (#50)
W_ML = 30  # sinal de alta probabilidade prevista pelo modelo ML

# ── ML ──────────────────────────────────────────────────────────────────
# O score do modelo é um ÍNDICE de risco relativo (similaridade ao perfil de
# quem já cancelou), não uma probabilidade calibrada de churn mensal — o rótulo
# de treino é "já cancelou alguma vez vs. ativo" (base ~55% churned por efeito
# das aquisições), então um corte absoluto de 0,5 acendia ~25% da base ativa e
# ficava inacionável. Por isso o sinal dispara de forma RELATIVA: só o topo da
# distribuição da própria base, respeitando um piso mínimo de índice.
ML_SIGNAL_FLOOR = Decimal("0.5")  # piso: abaixo disto o sinal nunca dispara
ML_FLAG_TOP_FRACTION = 0.10  # dispara só p/ o ~topo 10% da base ativa pontuada

# ── Limiares ────────────────────────────────────────────────────────────
BLOCKED_MIN_DAYS = 30
LATE_PAYMENTS_WINDOW_DAYS = 180  # ~6 meses
LATE_PAYMENTS_MIN = 3
TICKETS_WINDOW_DAYS = 30
TICKETS_MIN = 3
# Queda de banda: janela recente vs janela anterior de mesmo tamanho.
BANDWIDTH_WINDOW_DAYS = 30
BANDWIDTH_DROP_RATIO = Decimal("0.3")  # recente < 30% do anterior = queda ≥ 70%
BANDWIDTH_MIN_PRIOR_BYTES = 1_000_000_000  # 1 GB — evita ruído de base ínfima
# Insatisfação: dispara só acima de meio índice — fração relevante de mensagens
# negativas e/ou nota likert baixa. Abaixo disso é ruído de conversa pontual.
DISSATISFACTION_MIN = 0.5

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
    # (source_type, external_id) → customer_id — base pro sinal de downgrade.
    contract_keys: dict[tuple[str, str], int] = {}

    # ── Receita em risco + população relevante ──────────────────────────
    # Só contratos ACTIVE/BLOCKED geram MRR — base pra "receita em risco".
    for c in Contract.objects.filter(
        organization=organization, status__in=("ACTIVE", "BLOCKED")
    ):
        # Contrato sem cliente resolvido (sync chegou antes do Customer) não
        # pode ser atribuído a um risco — ignora até a FK ser preenchida.
        if c.customer_id is None:
            continue
        mrr[c.customer_id] += c.monthly_amount_net
        contract_keys[(c.source_type, c.external_id)] = c.customer_id
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

    # ── Sinal 3: chamados de suporte frequentes (≥ 3 nos últimos 30 dias) ─
    # Conta só chamados que indicam insatisfação com o serviço (problema
    # técnico/rede/suporte). Instalação, troca de equipamento, financeiro e
    # titularidade são rotina e não sinalizam churn — antes inflavam o número.
    ticket_cutoff = now - timedelta(days=TICKETS_WINDOW_DAYS)
    lookups = load_os_lookups(organization)
    relevant_subject_ids = churn_relevant_subject_ids(lookups.subject_map)
    ticket_qs = Ticket.objects.filter(
        organization=organization, opened_at__gte=ticket_cutoff
    )
    if relevant_subject_ids is not None:
        # Org com lookups sincronizados: restringe a assuntos de suporte.
        ticket_qs = ticket_qs.filter(subject_id__in=relevant_subject_ids)
    # Sem lookups (None): mantém comportamento antigo (conta todos) — fallback.
    ticket_rows = (
        ticket_qs.values("customer_id")
        .annotate(n=Count("id"))
        .filter(n__gte=TICKETS_MIN)
    )
    for row in ticket_rows:
        cid = row["customer_id"]
        if cid is None:
            continue
        signals[cid].append({
            "code": "FREQUENT_TICKETS",
            "label": "Chamados de suporte frequentes",
            "detail": f"{row['n']} chamados de suporte nos últimos 30 dias",
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

    # ── Sinal 5: downgrade de plano (valor mensal caiu vs versão anterior) ─
    _apply_downgrade_signal(organization, contract_keys, signals)

    # ── Sinal 6: queda brusca de consumo de banda ───────────────────────
    _apply_bandwidth_drop_signal(organization, today, active_customers, signals)

    # ── Sinal 7: insatisfação nas conversas (léxico × likert) ───────────
    _apply_dissatisfaction_signal(organization, set(mrr.keys()), signals)

    # ── Sinal 8: ML — alta probabilidade prevista de churn ──────────────
    # Complementar às regras: pode flagar clientes ativos sem sinal de regra.
    ml_probs = _apply_ml_signal(organization, active_customers, signals)

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
        prob = ml_probs.get(cid)
        ChurnRiskScore.objects.update_or_create(
            organization=organization,
            customer_id=cid,
            defaults={
                "score": score,
                "level": level,
                "signals": clean,
                "monthly_amount": mrr[cid],
                "ml_probability": (
                    Decimal(str(round(prob, 4))) if prob is not None else None
                ),
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


def _apply_downgrade_signal(
    organization: Organization,
    contract_keys: dict[tuple[str, str], int],
    signals: dict[int, list[dict[str, Any]]],
) -> None:
    """Dispara o sinal de downgrade comparando a versão SCD2 atual do contrato
    com a versão imediatamente anterior em DimContract.

    Downgrade = monthly_amount da versão `current` < monthly_amount da versão
    anterior (maior `valid_from` entre as não-current). Reduzir o plano é um
    sinal clássico de cliente prestes a sair.
    """
    from apps.analytics.infrastructure.models import DimContract

    if not contract_keys:
        return

    external_ids = {ext for (_src, ext) in contract_keys}
    # Agrupa versões por (source_type, external_id), ordenadas por valid_from.
    versions: dict[tuple[str, str], list[DimContract]] = defaultdict(list)
    for dim in (
        DimContract.objects.filter(
            organization=organization, external_id__in=external_ids
        )
        .only("source_type", "external_id", "monthly_amount", "current", "valid_from")
        .order_by("valid_from")
    ):
        versions[(dim.source_type, dim.external_id)].append(dim)

    for key, customer_id in contract_keys.items():
        history = versions.get(key)
        if not history or len(history) < 2:
            continue
        current = next((d for d in history if d.current), history[-1])
        prior = [d for d in history if d is not current]
        if not prior:
            continue
        # Versão anterior = a de maior valid_from entre as restantes.
        previous = max(prior, key=lambda d: d.valid_from)
        if current.monthly_amount >= previous.monthly_amount:
            continue
        signals[customer_id].append({
            "code": "PLAN_DOWNGRADE",
            "label": "Downgrade de plano",
            "detail": (
                f"Plano reduziu de R$ {previous.monthly_amount} "
                f"para R$ {current.monthly_amount}"
            ),
            "weight": W_DOWNGRADE,
        })


def _apply_bandwidth_drop_signal(
    organization: Organization,
    today: Any,
    active_customers: set[int],
    signals: dict[int, list[dict[str, Any]]],
) -> None:
    """Dispara o sinal de queda brusca de consumo comparando a banda usada na
    janela recente (30d) com a janela anterior de mesmo tamanho (30–60d).

    Queda ≥ 70% (recente < 30% do anterior), exigindo base mínima na janela
    anterior pra evitar ruído. Só clientes ativos — quem já saiu não interessa.
    """
    from apps.network.infrastructure.models import BandwidthUsage

    if not active_customers:
        return

    recent_start = today - timedelta(days=BANDWIDTH_WINDOW_DAYS)
    prior_start = today - timedelta(days=2 * BANDWIDTH_WINDOW_DAYS)

    recent: dict[int, int] = defaultdict(int)
    prior: dict[int, int] = defaultdict(int)
    for row in (
        BandwidthUsage.objects.filter(
            organization=organization,
            customer_id__in=active_customers,
            reference_date__gte=prior_start,
            reference_date__lt=today,
        ).values("customer_id", "reference_date", "download_bytes", "upload_bytes")
    ):
        cid = row["customer_id"]
        total = (row["download_bytes"] or 0) + (row["upload_bytes"] or 0)
        if row["reference_date"] >= recent_start:
            recent[cid] += total
        else:
            prior[cid] += total

    for cid, prior_bytes in prior.items():
        if prior_bytes < BANDWIDTH_MIN_PRIOR_BYTES:
            continue
        recent_bytes = recent.get(cid, 0)
        if recent_bytes >= prior_bytes * BANDWIDTH_DROP_RATIO:
            continue
        drop_pct = int((1 - Decimal(recent_bytes) / Decimal(prior_bytes)) * 100)
        signals[cid].append({
            "code": "BANDWIDTH_DROP",
            "label": "Queda de consumo",
            "detail": f"Consumo de banda caiu {drop_pct}% no último mês",
            "weight": W_BANDWIDTH_DROP,
        })


def _apply_dissatisfaction_signal(
    organization: Organization,
    eligible: set[int],
    signals: dict[int, list[dict[str, Any]]],
) -> None:
    """Dispara o sinal de insatisfação a partir do score de conversas (#50).

    Score = fração de mensagens do cliente com tom negativo (léxico PT-BR)
    combinada com nota likert baixa. Só dispara para clientes com receita em
    risco (ACTIVE/BLOCKED) acima do limiar — conversa pontual ruim não conta.
    """
    from apps.analytics.application.dissatisfaction import (
        compute_dissatisfaction_scores,
    )

    if not eligible:
        return
    scores = compute_dissatisfaction_scores(organization)
    for cid, score in scores.items():
        if cid not in eligible or score < DISSATISFACTION_MIN:
            continue
        signals[cid].append({
            "code": "DISSATISFACTION",
            "label": "Insatisfação nas conversas",
            "detail": (
                f"Índice de insatisfação {int(score * 100)}/100 — tom negativo "
                f"nas mensagens e/ou nota baixa de atendimento"
            ),
            "weight": W_DISSATISFACTION,
        })


def _apply_ml_signal(
    organization: Organization,
    active_customers: set[int],
    signals: dict[int, list[dict[str, Any]]],
) -> dict[int, float]:
    """Pontua os clientes ativos com o modelo ML (se houver) e dispara o sinal
    de ML para os de alta probabilidade.

    Retorna {customer_id → probabilidade} pra gravar `ml_probability` na
    persistência. Sem modelo treinado, retorna vazio (fallback para regras).
    """
    from apps.analytics.application.churn_ml import (
        compute_features,
        get_current_model,
        predict_probabilities,
    )

    if not active_customers:
        return {}
    model = get_current_model(organization)
    if model is None:
        return {}

    features_all, _churned, _active = compute_features(organization)
    features = {
        cid: vec for cid, vec in features_all.items() if cid in active_customers
    }
    probs = predict_probabilities(model, features)
    if not probs:
        return probs

    # Limiar RELATIVO: o maior entre o piso e o índice do corte de topo
    # (percentil 1 - top_fraction). Mantém o sinal preso ao ~topo 10% da base,
    # em vez de acender em todo cliente acima de um 0,5 absoluto.
    floor = float(ML_SIGNAL_FLOOR)
    ordered = sorted(probs.values(), reverse=True)
    cut_idx = max(0, int(len(ordered) * ML_FLAG_TOP_FRACTION) - 1)
    threshold = max(floor, ordered[cut_idx])

    for cid, prob in probs.items():
        if prob < threshold:
            continue
        signals[cid].append({
            "code": "ML_HIGH_RISK",
            "label": "ML: alto índice de risco",
            "detail": (
                f"Índice de risco ML {int(prob * 100)}/100 — perfil entre os de "
                f"maior risco da base (parecido com quem já cancelou)"
            ),
            "weight": W_ML,
        })
    return probs
