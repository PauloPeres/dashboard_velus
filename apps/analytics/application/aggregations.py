"""Agregações analíticas — MRR, churn, ARPU, inadimplência, cash forecast.

Operam sobre fact tables, retornam dicts simples (JSON-friendly) prontos pra
view/template/Plotly. NUNCA tocam domain models diretamente.

Cache: cada função aceita `use_cache=True` e usa Redis com TTL+invalidação
por signal `sync_completed` (futuro).
"""

from __future__ import annotations

import math
import statistics
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, DecimalField, Max, OuterRef, Q, Subquery, Sum
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from apps.analytics.infrastructure.models import (
    ChurnRiskScore,
    DimContract,
    FactContractStatusDaily,
    FactExpense,
    FactInvoice,
    FactPayment,
)
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_ZERO = Decimal("0.00")

# Contratos cancelados: status explícito OU UNKNOWN com data de cancelamento definida.
# Defensivo contra re-syncs que revertem CANCELED → UNKNOWN (bug histórico do adapter IXC).
_CANCELED_Q = Q(status="CANCELED") | Q(status="UNKNOWN", canceled_at__isnull=False)


def _first_of_month_n_ago(base: date_cls, n: int) -> date_cls:
    """Retorna o primeiro dia do calendário N meses antes do mês de `base`.

    Usa aritmética de calendário real — evita bugs com timedelta(days=n*30)
    que produz meses duplicados quando os meses têm 28-31 dias.
    """
    month = base.month - n
    year = base.year
    while month <= 0:
        month += 12
        year -= 1
    return base.replace(year=year, month=month, day=1)


def _full_month_keys(cutoff: date_cls, until: date_cls) -> list[str]:
    """Lista contígua de 'YYYY-MM' do mês de `cutoff` até o mês de `until`.

    Garante eixo temporal completo mesmo em meses sem lançamento — quem consome
    preenche os buracos com 0.0.
    """
    keys: list[str] = []
    y, m = cutoff.year, cutoff.month
    end_y, end_m = until.year, until.month
    while (y, m) <= (end_y, end_m):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return keys


def _ols_fit(ys: list[float]) -> tuple[float, float]:
    """Regressão linear simples (mínimos quadrados) sobre x = 0,1,2,...

    Retorna (slope, intercept) na forma fechada — sem numpy. Quando há menos de
    dois pontos ou variância zero em x, devolve slope 0 e intercept = média.
    """
    n = len(ys)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return 0.0, ys[0]
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return 0.0, mean_y
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _seasonal_factors(
    month_numbers: list[int],
    values: list[float],
    fitted: list[float],
    damping: float = 0.5,
) -> dict[int, float]:
    """Fatores sazonais multiplicativos por mês-do-ano (1..12), amortecidos.

    `month_numbers`, `values` e `fitted` são pareados na mesma ordem cronológica.
    Para cada mês-do-ano agrega a razão média observado/tendência e amortece em
    direção a 1.0 (damping=0.5 → metade do desvio). Como só temos ~1 ano de
    histórico, o damping evita superajuste a um único mês observado.
    """
    by_month: dict[int, list[float]] = {}
    for mn, v, f in zip(month_numbers, values, fitted):
        if f <= 0:
            continue
        by_month.setdefault(mn, []).append(v / f)
    factors: dict[int, float] = {}
    for mn, ratios in by_month.items():
        avg = sum(ratios) / len(ratios)
        # Amortece: 1.0 + damping*(avg-1.0). damping=0 → sem sazonalidade.
        factors[mn] = 1.0 + damping * (avg - 1.0)
    return factors


def _ixc_blocked_since(raw_extras: dict[str, Any] | None) -> date_cls | None:
    """Data em que o contrato foi bloqueado, segundo o ERP IXC.

    O `FactContractStatusDaily` só sabe quando o contrato ficou bloqueado se há
    histórico de um dia NÃO bloqueado anterior. Contratos já bloqueados antes do
    início dos snapshots não têm essa âncora — caem no sentinela 999 dias.
    O IXC guarda a data real em `dt_ult_bloq_manual`/`dt_ult_bloq_auto`; quando
    o bloqueio é automático esses campos vêm vazios, mas `data_inicial_suspensao`
    marca o início da suspensão. Usamos a mais recente como fallback autoritativo.
    Datas nulas do IXC chegam como ''/'0000-00-00' e são descartadas.
    """
    candidates: list[date_cls] = []
    for key in ("dt_ult_bloq_manual", "dt_ult_bloq_auto", "data_inicial_suspensao"):
        value = str((raw_extras or {}).get(key) or "").strip()
        if not value:
            continue
        try:
            candidates.append(date_cls.fromisoformat(value[:10]))
        except ValueError:
            continue
    return max(candidates) if candidates else None


def _block_start_date(
    last_ok: date_cls | None, ixc_since: date_cls | None
) -> date_cls | None:
    """Data de início do bloqueio atual.

    Prefere o histórico de snapshots (dia seguinte ao último dia NÃO bloqueado)
    quando disponível; senão usa a data autoritativa do IXC.
    """
    if last_ok:
        return last_ok + timedelta(days=1)
    return ixc_since


def _days_blocked(
    today: date_cls, last_ok: date_cls | None, ixc_since: date_cls | None
) -> int:
    """Dias consecutivos bloqueado; 999 quando não há âncora de data."""
    start = _block_start_date(last_ok, ixc_since)
    return (today - start).days if start else 999


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP, escopo é arg explícito")
def compute_mrr_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Série mensal de MRR (Monthly Recurring Revenue).

    MRR mensal = soma de `monthly_amount` dos contratos ativos no último dia do mês.
    Retorna lista de `{month: 'YYYY-MM', mrr: Decimal}`.
    """
    today = timezone.now().date()
    series: list[dict[str, Any]] = []
    for i in range(months - 1, -1, -1):
        # Aritmética de calendário real — timedelta(days=30) duplica meses curtos
        target_month_first = _first_of_month_n_ago(today, i)
        if i > 0:
            sample_date = _first_of_month_n_ago(today, i - 1) - timedelta(days=1)
        else:
            sample_date = today

        result = FactContractStatusDaily.objects.filter(
            organization=organization,
            date=sample_date,
            is_active=True,
        ).aggregate(
            mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
            active=Count("id"),
        )
        series.append(
            {
                "month": target_month_first.strftime("%Y-%m"),
                "label": target_month_first.strftime("%b/%y"),
                "mrr": float(result["mrr"] or 0),
                "active_contracts": result["active"] or 0,
            }
        )
    return series


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_kpis(organization: Organization) -> dict[str, Any]:
    """KPIs principais pra cards no topo do dashboard."""
    today = timezone.now().date()
    month_first = today.replace(day=1)
    last_month_first = (month_first - timedelta(days=1)).replace(day=1)

    # MRR atual e mês anterior
    mrr_now = (
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, is_active=True
        ).aggregate(s=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()))
    )["s"] or _ZERO

    last_day_prev = month_first - timedelta(days=1)
    mrr_prev = (
        FactContractStatusDaily.objects.filter(
            organization=organization, date=last_day_prev, is_active=True
        ).aggregate(s=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()))
    )["s"] or _ZERO

    mrr_delta_pct = (
        float((mrr_now - mrr_prev) / mrr_prev * 100) if mrr_prev > 0 else 0.0
    )

    # Contratos ativos — is_active = status in (ACTIVE, BLOCKED, AWAITING_INSTALL)
    status_breakdown = (
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, is_active=True
        )
        .values("status")
        .annotate(n=Count("id"))
    )
    breakdown = {row["status"]: row["n"] for row in status_breakdown}
    active_count = sum(breakdown.values())

    # Novos no mês (ativos hoje mas não tinham status no início do mês anterior)
    # Aproximação: contratos com activated_at no mês corrente
    from apps.customers.infrastructure.models import Contract
    new_count = Contract.objects.filter(
        organization=organization,
        activated_at__gte=month_first,
    ).count()

    canceled_count = Contract.objects.filter(
        organization=organization,
        canceled_at__gte=month_first,
    ).count()

    # Churn = cancelados no mês / ativos no início do mês * 100
    active_at_start = FactContractStatusDaily.objects.filter(
        organization=organization, date=last_month_first, is_active=True
    ).count()
    churn_pct = (
        float(canceled_count / active_at_start * 100) if active_at_start > 0 else 0.0
    )

    # Inadimplência
    delinquency = FactInvoice.objects.filter(
        organization=organization,
        status__in=("PENDING", "OVERDUE"),
        days_overdue__gt=0,
    ).aggregate(
        total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
        count=Count("id"),
    )
    delinquency_amount = float(delinquency["total"] or 0)
    delinquency_pct_of_mrr = (
        float(delinquency["total"] / mrr_now * 100) if mrr_now > 0 else 0.0
    )

    return {
        "mrr_now": float(mrr_now),
        "mrr_prev": float(mrr_prev),
        "mrr_delta_pct": mrr_delta_pct,
        "active_contracts": active_count,
        "active_only": breakdown.get("ACTIVE", 0),
        "blocked_contracts": breakdown.get("BLOCKED", 0),
        "awaiting_contracts": breakdown.get("AWAITING_INSTALL", 0),
        "new_this_month": new_count,
        "canceled_this_month": canceled_count,
        "churn_pct": churn_pct,
        "delinquency_amount": delinquency_amount,
        "delinquency_count": delinquency["count"] or 0,
        "delinquency_pct_of_mrr": delinquency_pct_of_mrr,
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_aging_distribution(organization: Organization) -> list[dict[str, Any]]:
    """Distribuição de inadimplência por bucket de aging."""
    by_bucket = (
        FactInvoice.objects.filter(
            organization=organization,
            status__in=("PENDING", "OVERDUE"),
        )
        .values("aging_bucket")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
    )
    bucket_labels = {
        "ON_TIME": "Em dia",
        "0_30": "1–30 dias",
        "31_60": "31–60 dias",
        "61_90": "61–90 dias",
        "OVER_90": "90+ dias",
    }
    order = ["ON_TIME", "0_30", "31_60", "61_90", "OVER_90"]
    result_by_key = {row["aging_bucket"]: row for row in by_bucket}
    return [
        {
            "key": k,
            "label": bucket_labels.get(k, k),
            "amount": float(result_by_key.get(k, {}).get("total", 0) or 0),
            "count": result_by_key.get(k, {}).get("count", 0) or 0,
        }
        for k in order
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_arpu_by_plan(organization: Organization) -> list[dict[str, Any]]:
    """ARPU por plano — receita média / contratos ativos por nome do plano."""
    today = timezone.now().date()
    by_plan = (
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, is_active=True
        )
        .values("contract__plan_name")
        .annotate(
            revenue=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("-revenue")
    )
    return [
        {
            "plan": row["contract__plan_name"] or "—",
            "revenue": float(row["revenue"]),
            "count": row["count"],
            "arpu": float(row["revenue"] / row["count"]) if row["count"] > 0 else 0.0,
        }
        for row in by_plan
    ]


def _mrr_active_at(organization: Organization, sample_date: date_cls) -> tuple[float, int]:
    """(MRR, contratos ativos) num dia — base de snapshot do FactContractStatusDaily."""
    agg = FactContractStatusDaily.objects.filter(
        organization=organization, date=sample_date, is_active=True
    ).aggregate(
        mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
        active=Count("id"),
    )
    return float(agg["mrr"] or 0), agg["active"] or 0


def _pct_delta(current: float, previous: float) -> float:
    """Variação percentual; 0.0 quando não há base anterior."""
    return (current - previous) / previous * 100 if previous else 0.0


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_revenue_comparison(organization: Organization) -> list[dict[str, Any]]:
    """Métricas mês atual × mês anterior pra cards comparativos em /revenue/.

    Cobre MRR, ARPU (MRR ÷ contratos ativos), net adds (novos − cancelados) e
    receita recebida (caixa). Cada item traz valor atual, valor do mês anterior,
    variação absoluta e percentual, além de metadados de formatação/cor.

    `higher_is_better` define a cor do delta no card (verde = bom). Para todas as
    métricas aqui crescer é positivo.
    """
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()
    month_first = today.replace(day=1)
    prev_month_last = month_first - timedelta(days=1)
    prev_month_first = prev_month_last.replace(day=1)

    # MRR / ARPU: snapshot de hoje vs último dia do mês anterior.
    mrr_now, active_now = _mrr_active_at(organization, today)
    mrr_prev, active_prev = _mrr_active_at(organization, prev_month_last)
    arpu_now = mrr_now / active_now if active_now else 0.0
    arpu_prev = mrr_prev / active_prev if active_prev else 0.0

    # Net adds: novos − cancelados, dentro de cada janela mensal.
    # `activated_at`/`canceled_at` são DateTimeField → usa bounds tz-aware.
    def _aware(d: date_cls) -> Any:
        return timezone.make_aware(datetime.combine(d, time.min))

    def _net_adds(start: date_cls, end: date_cls | None) -> int:
        start_dt = _aware(start)
        new_q = Contract.objects.filter(organization=organization, activated_at__gte=start_dt)
        can_q = Contract.objects.filter(organization=organization, canceled_at__gte=start_dt)
        if end is not None:
            end_dt = _aware(end)
            new_q = new_q.filter(activated_at__lt=end_dt)
            can_q = can_q.filter(canceled_at__lt=end_dt)
        return new_q.count() - can_q.count()

    net_now = _net_adds(month_first, None)
    net_prev = _net_adds(prev_month_first, month_first)

    # Receita recebida (caixa) por mês de pagamento.
    received = {r["month"]: r["amount"] for r in compute_cash_received_series(organization, months=3)}
    recv_now = float(received.get(month_first.strftime("%Y-%m"), 0.0))
    recv_prev = float(received.get(prev_month_first.strftime("%Y-%m"), 0.0))

    return [
        {
            "key": "mrr",
            "label": "MRR",
            "current": round(mrr_now, 2),
            "previous": round(mrr_prev, 2),
            "delta_abs": round(mrr_now - mrr_prev, 2),
            "delta_pct": round(_pct_delta(mrr_now, mrr_prev), 1),
            "fmt": "brl",
            "higher_is_better": True,
        },
        {
            "key": "arpu",
            "label": "ARPU (ticket médio)",
            "current": round(arpu_now, 2),
            "previous": round(arpu_prev, 2),
            "delta_abs": round(arpu_now - arpu_prev, 2),
            "delta_pct": round(_pct_delta(arpu_now, arpu_prev), 1),
            "fmt": "brl",
            "higher_is_better": True,
        },
        {
            "key": "net_adds",
            "label": "Net Adds (novos − cancelados)",
            "current": net_now,
            "previous": net_prev,
            "delta_abs": net_now - net_prev,
            "delta_pct": round(_pct_delta(net_now, net_prev), 1),
            "fmt": "int",
            "higher_is_better": True,
        },
        {
            "key": "received",
            "label": "Receita Recebida (caixa)",
            "current": round(recv_now, 2),
            "previous": round(recv_prev, 2),
            "delta_abs": round(recv_now - recv_prev, 2),
            "delta_pct": round(_pct_delta(recv_now, recv_prev), 1),
            "fmt": "brl",
            "higher_is_better": True,
        },
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_pipeline_by_status(organization: Organization) -> list[dict[str, Any]]:
    """Distribuição de contratos por status (a partir do DimContract atual)."""
    by_status = (
        DimContract.objects.filter(organization=organization, current=True)
        .values("status")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [{"status": row["status"], "count": row["count"]} for row in by_status]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_cash_received_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Recebimentos por mês — entrada de caixa real.

    Usa FactInvoice com status='PAID' e paid_date preenchida (pagamento_data do IXC).
    Fallback para FactPayment se houver dados (futura integração de pagamentos direta).
    """
    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    # Prefere FactInvoice.paid_date — populado pelo IXC pagamento_data
    by_month = (
        FactInvoice.objects.filter(
            organization=organization,
            status="PAID",
            paid_date__gte=cutoff,
            paid_date__isnull=False,
        )
        .annotate(month=TruncMonth("paid_date"))
        .values("month")
        .annotate(
            total=Coalesce(Sum("paid_amount"), Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("month")
    )
    invoice_rows = list(by_month)

    # Adiciona FactPayment se existir (integração futura de pagamentos direta)
    fp_by_month: dict[str, float] = {}
    fp_qs = (
        FactPayment.objects.filter(organization=organization, paid_date__gte=cutoff)
        .annotate(month=TruncMonth("paid_date"))
        .values("month")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
    )
    for row in fp_qs:
        key = row["month"].strftime("%Y-%m")
        fp_by_month[key] = float(row["total"])

    result = []
    for row in invoice_rows:
        key = row["month"].strftime("%Y-%m")
        invoice_total = float(row["total"])
        # Merge: se FactPayment tem dados para esse mês, usa o maior (evita dupla contagem)
        payment_total = fp_by_month.pop(key, 0.0)
        total = max(invoice_total, payment_total)
        result.append({
            "month": key,
            "label": row["month"].strftime("%b/%y"),
            "amount": total,
            "count": row["count"],
        })

    # Meses só em FactPayment (sem FactInvoice nesse período)
    for key, total in sorted(fp_by_month.items()):
        try:
            d = date_cls.fromisoformat(key + "-01")
            label = d.strftime("%b/%y")
        except ValueError:
            label = key
        result.append({"month": key, "label": label, "amount": total, "count": 0})

    return sorted(result, key=lambda x: x["month"])


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_open_revenue_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Receita NÃO recebida (em aberto) por mês de vencimento.

    Faturas com status PENDING/OVERDUE agrupadas pelo mês de `due_date` — o
    período em que a receita deveria ter entrado mas segue em aberto. Espelha
    `compute_cash_received_series` (recebido) pra montar o split do DRE.
    """
    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    by_month = (
        FactInvoice.objects.filter(
            organization=organization,
            status__in=("PENDING", "OVERDUE"),
            due_date__gte=cutoff,
        )
        .annotate(month=TruncMonth("due_date"))
        .values("month")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("month")
    )
    return [
        {
            "month": row["month"].strftime("%Y-%m"),
            "label": row["month"].strftime("%b/%y"),
            "amount": float(row["total"]),
            "count": row["count"],
        }
        for row in by_month
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_expense_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Despesas pagas por mês (expense_date)."""
    today = timezone.now().date()
    cutoff = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    by_month = (
        FactExpense.objects.filter(
            organization=organization,
            status="PAID",
            expense_date__gte=cutoff,
        )
        .annotate(month=TruncMonth("expense_date"))
        .values("month")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("month")
    )
    return [
        {
            "month": row["month"].strftime("%Y-%m"),
            "label": row["month"].strftime("%b/%y"),
            "expenses": float(row["total"]),
            "count": row["count"],
        }
        for row in by_month
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_cashflow_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Fluxo de caixa: receita recebida - despesas pagas por mês."""
    revenue_series = compute_cash_received_series(organization, months=months)
    expense_series = compute_expense_series(organization, months=months)

    # Build lookup by month key
    rev_by_month = {r["month"]: r["amount"] for r in revenue_series}
    exp_by_month = {e["month"]: e["expenses"] for e in expense_series}

    # Union of all months from both series
    all_months: set[str] = set(rev_by_month) | set(exp_by_month)
    if not all_months:
        return []

    # Build sorted series
    from datetime import date as date_cls
    sorted_months = sorted(all_months)
    result = []
    cumulative = 0.0
    for month_key in sorted_months:
        revenue = rev_by_month.get(month_key, 0.0)
        expenses = exp_by_month.get(month_key, 0.0)
        net = revenue - expenses
        cumulative += net
        # Parse label from month_key YYYY-MM
        try:
            d = date_cls.fromisoformat(month_key + "-01")
            label = d.strftime("%b/%y")
        except ValueError:
            label = month_key
        result.append(
            {
                "month": month_key,
                "label": label,
                "revenue": revenue,
                "expenses": expenses,
                "net": net,
                "cumulative_net": cumulative,
            }
        )
    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_expense_by_supplier(
    organization: Organization, months: int = 3
) -> list[dict[str, Any]]:
    """Top fornecedores por despesa paga nos últimos N meses.

    Lê `Expense.supplier_name` diretamente (não a cópia desnormalizada em
    FactExpense) para garantir que backfills e sincronizações recentes já
    estejam refletidos. Exclui nomes no formato 'Fornecedor #XXX' que
    indicam fornecedores ainda não resolvidos pela API IXC.
    """
    from apps.financial.infrastructure.models import Expense as ExpenseModel

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)
    qs = (
        ExpenseModel.objects.filter(
            organization=organization,
            status="PAID",
            paid_at__isnull=False,
            paid_at__gte=cutoff,
        )
        .exclude(supplier_name__startswith="Fornecedor #")
        .exclude(supplier_name="")
        .values("supplier_name")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("-total")[:20]
    )
    return [
        {
            "supplier": row["supplier_name"],
            "amount": float(row["total"]),
            "count": row["count"],
        }
        for row in qs
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_expense_by_category(
    organization: Organization, months: int = 3
) -> list[dict[str, Any]]:
    """Distribuição de despesas pagas por categoria IXC (planejamento) nos últimos N meses.

    Usa `id_conta` (singular) do campo `raw_extras` da tabela Expense — a conta
    analítica (planejamento_analitico.id) que o usuário vê no sistema IXC.
    NÃO usa `id_contas` (plural) que é a conta bancária de pagamento.
    """
    from apps.financial.infrastructure.models import Expense as ExpenseModel

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)
    qs = (
        ExpenseModel.objects.filter(
            organization=organization,
            status="PAID",
            paid_at__isnull=False,
            paid_at__gte=cutoff,
        )
        .annotate(id_conta_str=KeyTextTransform("id_conta", "raw_extras"))
        .values("id_conta_str")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("-total")
    )
    conta_map, plano_map = _load_plano_maps(organization)
    # Agrupa por id_planejamento (conta pai) — múltiplos id_conta podem ter o mesmo pai
    parent_map: dict[str, dict[str, Any]] = {}
    for row in qs:
        id_conta = str(row["id_conta_str"] or "0")
        id_plan = conta_map.get(id_conta, "0")
        entry = plano_map.get(id_plan, {})
        label = entry.get("nome") or f"Conta #{id_plan}"
        if label not in parent_map:
            parent_map[label] = {"category": label, "amount": 0.0, "count": 0}
        parent_map[label]["amount"] += float(row["total"])
        parent_map[label]["count"] += row["count"]
    return sorted(parent_map.values(), key=lambda x: -x["amount"])


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_burn_rate(
    organization: Organization, months: int = 6
) -> dict[str, Any]:
    """Burn rate: média mensal de despesas pagas + série histórica.

    burn_rate = média das despesas pagas nos últimos 3 meses.
    trend_pct = variação % entre o mês mais antigo e o mais recente dos últimos 3m.
    """
    series = compute_expense_series(organization, months=months)
    burn_series = [{"month": s["month"], "label": s["label"], "expenses": s["expenses"]} for s in series]

    # Burn rate = avg of last 3 paid months
    paid_months = [s["expenses"] for s in series if s["expenses"] > 0]
    last_3 = paid_months[-3:] if len(paid_months) >= 3 else paid_months
    burn_rate = sum(last_3) / len(last_3) if last_3 else 0.0

    # Trend %: diff between first and last of the 3 months
    if len(last_3) >= 2:
        trend_pct = float((last_3[-1] - last_3[0]) / last_3[0] * 100) if last_3[0] > 0 else 0.0
    else:
        trend_pct = 0.0

    return {
        "burn_rate": burn_rate,
        "burn_series": burn_series,
        "trend_pct": trend_pct,
        "months_sampled": len(last_3),
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_dre(
    organization: Organization, months: int = 12
) -> dict[str, Any]:
    """DRE Gerencial simplificado.

    Combina MRR (receita contratada) + recebimentos reais + despesas para
    montar um DRE executivo de alto nível.

    Returns dict with:
      - mrr_series: list[dict] (month, label, mrr)
      - expense_series: list[dict] (month, label, expenses)
      - cashflow_series: list[dict] (month, label, revenue, expenses, net)
      - current_month: {receita_bruta, despesas, ebitda, ebitda_margin_pct}
      - ytd: {receita_bruta, despesas, ebitda}
    """
    mrr_series = compute_mrr_series(organization, months=months)
    expense_series = compute_expense_series(organization, months=months)
    cashflow_series = compute_cashflow_series(organization, months=months)
    received_series = compute_cash_received_series(organization, months=months)
    open_series = compute_open_revenue_series(organization, months=months)

    received_by_month = {r["month"]: r["amount"] for r in received_series}
    open_by_month = {o["month"]: o["amount"] for o in open_series}

    # Série combinada de receita: contratada (MRR) × recebida × em aberto, por mês.
    revenue_series = [
        {
            "month": m["month"],
            "label": m["label"],
            "mrr": m["mrr"],
            "received": received_by_month.get(m["month"], 0.0),
            "open": open_by_month.get(m["month"], 0.0),
        }
        for m in mrr_series
    ]

    # Current month (last entry)
    current_mrr = mrr_series[-1]["mrr"] if mrr_series else 0.0
    exp_by_month = {e["month"]: e["expenses"] for e in expense_series}
    current_month_key = mrr_series[-1]["month"] if mrr_series else ""
    current_expenses = exp_by_month.get(current_month_key, 0.0)
    current_received = received_by_month.get(current_month_key, 0.0)
    current_open = open_by_month.get(current_month_key, 0.0)
    current_ebitda = current_mrr - current_expenses
    current_margin = (
        float(current_ebitda / current_mrr * 100) if current_mrr > 0 else 0.0
    )

    # YTD: sum only months in the current year
    current_year = str(timezone.now().year)
    ytd_revenue = sum(m["mrr"] for m in mrr_series if m["month"].startswith(current_year))
    ytd_expenses = sum(e["expenses"] for e in expense_series if e["month"].startswith(current_year))
    ytd_received = sum(r["amount"] for r in received_series if r["month"].startswith(current_year))
    ytd_open = sum(o["amount"] for o in open_series if o["month"].startswith(current_year))
    ytd_ebitda = ytd_revenue - ytd_expenses

    return {
        "mrr_series": mrr_series,
        "expense_series": expense_series,
        "cashflow_series": cashflow_series,
        "received_series": received_series,
        "open_series": open_series,
        "revenue_series": revenue_series,
        "current_month": {
            "receita_bruta": current_mrr,
            "receita_recebida": current_received,
            "receita_em_aberto": current_open,
            "despesas": current_expenses,
            "ebitda": current_ebitda,
            "ebitda_margin_pct": current_margin,
        },
        "ytd": {
            "receita_bruta": ytd_revenue,
            "receita_recebida": ytd_received,
            "receita_em_aberto": ytd_open,
            "despesas": ytd_expenses,
            "ebitda": ytd_ebitda,
        },
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_revenue_forecast(
    organization: Organization, months_ahead: int = 12
) -> list[dict[str, Any]]:
    """Previsão Nm com tendência linear (OLS), sazonalidade e cenários.

    Modelo (puro Python, sem numpy/sklearn — cluster k3s enxuto):

    * **Tendência**: regressão linear por mínimos quadrados sobre até 12 meses de
      MRR, em vez de crescimento composto de 3 meses (muito sensível a ruído).
    * **Sazonalidade**: fator multiplicativo por mês-do-ano (observado/tendência),
      amortecido para 1.0 — só temos ~1 ano de dados, então não superajustamos.
    * **Taxa de recebimento variável**: OLS sobre a razão caixa/MRR mês a mês,
      projetada forward (clamp 0.50–1.05) em vez de constante.
    * **Despesas com tendência**: OLS sobre despesas mensais (fallback média 3m).
    * **Cenários**: banda otimista/pessimista a partir do desvio relativo dos
      resíduos, alargando com o horizonte (`rel_std * sqrt(i+1)`).

    Mantém as chaves usadas por view/chart/template (`forecast_mrr`,
    `forecast_cash`, `forecast_expenses`, `forecast_net`, `collection_rate_pct`,
    `is_forecast`, `month`, `label`) e acrescenta as variantes de cenário.
    Quando há < 4 meses de histórico cai no modelo ingênuo de crescimento composto.
    """
    hist_mrr = compute_mrr_series(organization, months=12)
    hist_exp = compute_expense_series(organization, months=12)
    hist_cash = compute_cash_received_series(organization, months=12)

    mrr_points = [(m["month"], m["mrr"]) for m in hist_mrr]
    mrr_values = [v for _, v in mrr_points]

    today = timezone.now().date()
    nm, ny = today.month + 1, today.year
    if nm > 12:
        nm, ny = 1, ny + 1
    forecast_start = date_cls(ny, nm, 1)  # próximo mês

    def _month_at(i: int) -> date_cls:
        m = forecast_start.month + i
        y = forecast_start.year + (m - 1) // 12
        return date_cls(y, ((m - 1) % 12) + 1, 1)

    # Modelo ingênuo de fallback quando o histórico é curto demais p/ OLS.
    if len([v for v in mrr_values if v > 0]) < 4:
        return _naive_revenue_forecast(
            hist_mrr, hist_exp, hist_cash, months_ahead, _month_at
        )

    # --- Tendência de MRR (OLS) ---
    slope, intercept = _ols_fit(mrr_values)
    n = len(mrr_values)
    fitted = [slope * x + intercept for x in range(n)]

    # --- Sazonalidade amortecida por mês-do-ano ---
    month_numbers = [int(key[5:7]) for key, _ in mrr_points]
    factors = _seasonal_factors(month_numbers, mrr_values, fitted, damping=0.5)

    # --- Resíduos relativos → largura da banda de cenários ---
    rel_residuals = [
        (mrr_values[i] - fitted[i]) / fitted[i]
        for i in range(n)
        if fitted[i] > 0
    ]
    rel_std = statistics.pstdev(rel_residuals) if len(rel_residuals) >= 2 else 0.0

    # --- Taxa de recebimento com tendência (OLS sobre caixa/MRR) ---
    mrr_by_month = {m["month"]: m["mrr"] for m in hist_mrr if m["mrr"] > 0}
    cash_by_month = {c["month"]: c["amount"] for c in hist_cash if c["amount"] > 0}
    common_months = sorted(set(mrr_by_month) & set(cash_by_month))
    rate_series = [cash_by_month[mo] / mrr_by_month[mo] for mo in common_months]
    if len(rate_series) >= 3:
        r_slope, r_intercept = _ols_fit(rate_series)
        nr = len(rate_series)

        def _rate_at(i: int) -> float:
            return max(0.50, min(1.05, r_slope * (nr - 1 + i + 1) + r_intercept))
    else:
        const_rate = (
            sum(rate_series[-3:]) / len(rate_series[-3:]) if rate_series else 1.0
        )
        const_rate = max(0.50, min(1.05, const_rate))

        def _rate_at(i: int) -> float:
            return const_rate

    # --- Despesas com tendência (OLS), fallback média 3m ---
    exp_values = [e["expenses"] for e in hist_exp]
    exp_nonzero = [v for v in exp_values if v > 0]
    if len(exp_nonzero) >= 4:
        e_slope, e_intercept = _ols_fit(exp_values)
        ne = len(exp_values)

        def _exp_at(i: int) -> float:
            return max(0.0, e_slope * (ne - 1 + i + 1) + e_intercept)
    else:
        avg_exp = (
            sum(exp_nonzero[-3:]) / len(exp_nonzero[-3:]) if exp_nonzero else 0.0
        )

        def _exp_at(i: int) -> float:
            return avg_exp

    result: list[dict[str, Any]] = []
    for i in range(months_ahead):
        d = _month_at(i)
        month_key = d.strftime("%Y-%m")
        label = d.strftime("%b/%y")

        trend_mrr = max(0.0, slope * (n - 1 + i + 1) + intercept)
        seasonal = factors.get(d.month, 1.0)
        forecast_mrr = trend_mrr * seasonal

        # Banda alarga com o horizonte (incerteza acumulada).
        band = rel_std * math.sqrt(i + 1)
        mrr_opt = forecast_mrr * (1 + band)
        mrr_pess = max(0.0, forecast_mrr * (1 - band))

        rate = _rate_at(i)
        exp = _exp_at(i)

        forecast_cash = forecast_mrr * rate
        forecast_net = forecast_cash - exp
        net_opt = mrr_opt * rate - exp
        net_pess = mrr_pess * rate - exp

        result.append(
            {
                "month": month_key,
                "label": label,
                "forecast_mrr": round(forecast_mrr, 2),
                "forecast_mrr_optimistic": round(mrr_opt, 2),
                "forecast_mrr_pessimistic": round(mrr_pess, 2),
                "forecast_cash": round(forecast_cash, 2),
                "forecast_expenses": round(exp, 2),
                "forecast_net": round(forecast_net, 2),
                "forecast_net_optimistic": round(net_opt, 2),
                "forecast_net_pessimistic": round(net_pess, 2),
                "seasonal_factor": round(seasonal, 4),
                "collection_rate_pct": round(rate * 100, 1),
                "is_forecast": True,
            }
        )
    return result


def _naive_revenue_forecast(
    hist_mrr: list[dict[str, Any]],
    hist_exp: list[dict[str, Any]],
    hist_cash: list[dict[str, Any]],
    months_ahead: int,
    month_at,
) -> list[dict[str, Any]]:
    """Fallback de crescimento composto p/ histórico curto (< 4 meses de MRR).

    Mantém o comportamento anterior — sem sazonalidade nem cenários — mas ainda
    emite as chaves de cenário (iguais ao valor base) para o chart/template não
    quebrarem.
    """
    mrr_values = [m["mrr"] for m in hist_mrr]
    if len(mrr_values) >= 3 and mrr_values[-3] > 0:
        growth = (mrr_values[-1] / mrr_values[-3]) ** (1 / 2) - 1
    elif len(mrr_values) >= 2 and mrr_values[0] > 0:
        growth = mrr_values[-1] / mrr_values[0] - 1
    else:
        growth = 0.0
    growth = max(-0.20, min(0.20, growth))

    mrr_by_month = {m["month"]: m["mrr"] for m in hist_mrr if m["mrr"] > 0}
    cash_by_month = {c["month"]: c["amount"] for c in hist_cash if c["amount"] > 0}
    common = sorted(set(mrr_by_month) & set(cash_by_month))
    rates = [cash_by_month[mo] / mrr_by_month[mo] for mo in common]
    collection_rate = sum(rates[-3:]) / len(rates[-3:]) if rates else 1.0
    collection_rate = max(0.50, min(1.05, collection_rate))

    exp_values = [e["expenses"] for e in hist_exp if e["expenses"] > 0]
    avg_exp = sum(exp_values[-3:]) / len(exp_values[-3:]) if exp_values else 0.0
    base_mrr = mrr_values[-1] if mrr_values else 0.0

    result: list[dict[str, Any]] = []
    for i in range(months_ahead):
        d = month_at(i)
        forecast_mrr = base_mrr * ((1 + growth) ** (i + 1))
        forecast_cash = forecast_mrr * collection_rate
        forecast_net = forecast_cash - avg_exp
        result.append(
            {
                "month": d.strftime("%Y-%m"),
                "label": d.strftime("%b/%y"),
                "forecast_mrr": round(forecast_mrr, 2),
                "forecast_mrr_optimistic": round(forecast_mrr, 2),
                "forecast_mrr_pessimistic": round(forecast_mrr, 2),
                "forecast_cash": round(forecast_cash, 2),
                "forecast_expenses": round(avg_exp, 2),
                "forecast_net": round(forecast_net, 2),
                "forecast_net_optimistic": round(forecast_net, 2),
                "forecast_net_pessimistic": round(forecast_net, 2),
                "seasonal_factor": 1.0,
                "collection_rate_pct": round(collection_rate * 100, 1),
                "is_forecast": True,
            }
        )
    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_delinquency_trend(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Inadimplência por mês de vencimento — quanto de cada coorte mensal ainda está em aberto.

    Para cada mês dos últimos N meses, soma o valor das faturas cujo `due_date` caiu
    naquele mês e que AINDA estão em aberto (PENDING ou OVERDUE, days_overdue > 0).

    Interpretação para ISP: "das faturas que venceram em Março, quantos reais ainda não
    foram pagos?" — mostra qual coorte de vencimento tem pior taxa de recuperação.
    """
    today = timezone.now().date()
    cutoff = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)

    by_month = (
        FactInvoice.objects.filter(
            organization=organization,
            status__in=("PENDING", "OVERDUE"),
            days_overdue__gt=0,
            due_date__gte=cutoff,
            due_date__lte=today,
        )
        .annotate(month=TruncMonth("due_date"))
        .values("month")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("month")
    )

    return [
        {
            "month": row["month"].strftime("%Y-%m"),
            "label": row["month"].strftime("%b/%y"),
            "amount": float(row["total"]),
            "count": row["count"],
        }
        for row in by_month
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_contract_status_trend(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Evolução mensal de contratos por status — snapshot do último dia disponível de cada mês.

    Retorna série de `{month, label, active, blocked, awaiting, total}` para
    alimentar gráfico de barras empilhadas.

    `is_active = status in (ACTIVE, BLOCKED, AWAITING_INSTALL)` — conforme definição do modelo.
    Contratos CANCELED e UNKNOWN são excluídos (is_active=False).
    """
    today = timezone.now().date()
    result: list[dict[str, Any]] = []

    for i in range(months - 1, -1, -1):
        # Aritmética de calendário real — timedelta(days=30) duplica meses curtos
        target_first = _first_of_month_n_ago(today, i)
        if i > 0:
            end_of_month = _first_of_month_n_ago(today, i - 1) - timedelta(days=1)
        else:
            end_of_month = today

        month_key = target_first.strftime("%Y-%m")
        label = target_first.strftime("%b/%y")

        # Usa o último dia disponível no mês (robusto caso sync não rodou no último dia)
        latest_date = FactContractStatusDaily.objects.filter(
            organization=organization,
            date__gte=target_first,
            date__lte=end_of_month,
            is_active=True,
        ).aggregate(latest=Max("date"))["latest"]

        if not latest_date:
            result.append({
                "month": month_key, "label": label,
                "active": 0, "blocked": 0, "awaiting": 0, "total": 0,
            })
            continue

        by_status = (
            FactContractStatusDaily.objects.filter(
                organization=organization,
                date=latest_date,
                is_active=True,
            )
            .values("status")
            .annotate(count=Count("id"))
        )
        counts = {row["status"]: row["count"] for row in by_status}
        active = counts.get("ACTIVE", 0)
        blocked = counts.get("BLOCKED", 0)
        awaiting = counts.get("AWAITING_INSTALL", 0)

        result.append({
            "month": month_key,
            "label": label,
            "active": active,
            "blocked": blocked,
            "awaiting": awaiting,
            "total": active + blocked + awaiting,
        })

    return result


# Recovery Rate — efetividade de recuperação de inadimplência.
# Janela de maturação: só consideramos coortes de vencimento com >=90 dias de
# idade, para dar tempo de a fatura ser paga em atraso antes de medir.
RECOVERY_MATURATION_DAYS = 90
RECOVERY_WINDOW_MONTHS = 12

_LATE_BUCKET_LABELS = {
    "0_30": "1–30 dias",
    "31_60": "31–60 dias",
    "61_90": "61–90 dias",
    "OVER_90": "90+ dias",
}
_LATE_BUCKET_ORDER = ("0_30", "31_60", "61_90", "OVER_90")


def _late_bucket(days: int) -> str:
    """Classifica dias de atraso no bucket de aging correspondente."""
    if days <= 30:
        return "0_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "OVER_90"


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_recovery_rate(organization: Organization) -> dict[str, Any]:
    """Recovery Rate — efetividade de recuperação de inadimplência.

    Definição (B): das faturas que inadimpliram (venceram sem pagamento em dia),
    quanto em R$ acabou sendo pago em atraso. Fonte: FactInvoice (binário pago/
    não pago — usa o status da fatura, não baixas parciais).

    Recovery Rate = valor recuperado em atraso / valor que inadimpliu.

    Janela: coortes de vencimento dos últimos 12 meses, restritas a faturas com
    pelo menos 90 dias de maturação (due_date <= today - 90d) — assim damos tempo
    de recuperação antes de medir e não penalizamos coortes recentes.

    O bucket de aging de cada fatura é a "profundidade do atraso":
      - recuperada: dias entre due_date e paid_date
      - em aberto: dias entre due_date e hoje
    Isso responde "quanto mais velho o atraso, menor a chance de recuperar?".
    """
    today = timezone.now().date()
    matured_before = today - timedelta(days=RECOVERY_MATURATION_DAYS)
    window_start = _first_of_month_n_ago(matured_before, RECOVERY_WINDOW_MONTHS - 1)

    rows = (
        FactInvoice.objects.filter(
            organization=organization,
            due_date__gte=window_start,
            due_date__lte=matured_before,
        )
        .exclude(status="CANCELED")
        .values("due_date", "paid_date", "status", "amount")
    )

    buckets: dict[str, dict[str, Any]] = {
        key: {"recovered": _ZERO, "total": _ZERO, "count": 0}
        for key in _LATE_BUCKET_ORDER
    }
    recovered_amount = _ZERO
    delinquent_amount = _ZERO
    outstanding_amount = _ZERO
    recovered_count = 0
    delinquent_count = 0

    for row in rows:
        due_date = row["due_date"]
        paid_date = row["paid_date"]
        status = row["status"]
        amount = row["amount"] or _ZERO

        is_paid = status == "PAID" and paid_date is not None
        # Paga em dia (ou adiantada) nunca inadimpliu — fora do denominador.
        if is_paid and paid_date <= due_date:
            continue

        if is_paid:
            # Recuperada: paga, porém em atraso.
            days_late = (paid_date - due_date).days
            bucket = _late_bucket(days_late)
            buckets[bucket]["recovered"] += amount
            buckets[bucket]["total"] += amount
            buckets[bucket]["count"] += 1
            recovered_amount += amount
            recovered_count += 1
        else:
            # Inadimpliu e segue em aberto — só entra no denominador.
            days_late = (today - due_date).days
            bucket = _late_bucket(days_late)
            buckets[bucket]["total"] += amount
            buckets[bucket]["count"] += 1
            outstanding_amount += amount

        delinquent_amount += amount
        delinquent_count += 1

    pct = (
        round(float(recovered_amount / delinquent_amount * 100), 1)
        if delinquent_amount > 0
        else 0.0
    )

    by_aging = []
    for key in _LATE_BUCKET_ORDER:
        b = buckets[key]
        b_total = b["total"]
        b_pct = (
            round(float(b["recovered"] / b_total * 100), 1) if b_total > 0 else 0.0
        )
        by_aging.append(
            {
                "key": key,
                "label": _LATE_BUCKET_LABELS[key],
                "recovered": float(b["recovered"]),
                "total": float(b_total),
                "pct": b_pct,
                "count": b["count"],
            }
        )

    return {
        "pct": pct,
        "recovered_amount": float(recovered_amount),
        "delinquent_amount": float(delinquent_amount),
        "outstanding_amount": float(outstanding_amount),
        "recovered_count": recovered_count,
        "delinquent_count": delinquent_count,
        "by_aging": by_aging,
        "window_months": RECOVERY_WINDOW_MONTHS,
        "maturation_days": RECOVERY_MATURATION_DAYS,
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_equipment_summary(organization: Organization) -> dict[str, Any]:
    """Resumo do parque de equipamentos em comodato (ONTs, roteadores, switches).

    Valor total em campo = soma de `value` dos equipamentos ACTIVE — o capital
    imobilizado emprestado a clientes, exposto a perda em cancelamentos sem
    devolução.
    """
    from apps.inventory.infrastructure.models import ContractEquipment

    by_status = (
        ContractEquipment.objects.filter(organization=organization)
        .values("status")
        .annotate(
            count=Count("id"),
            total=Coalesce(Sum("value"), _ZERO, output_field=DecimalField()),
        )
    )
    counts: dict[str, int] = {}
    values: dict[str, Decimal] = {}
    for row in by_status:
        counts[row["status"]] = row["count"]
        values[row["status"]] = row["total"]

    active_count = counts.get("ACTIVE", 0)
    active_value = values.get("ACTIVE", _ZERO)
    total_count = sum(counts.values())

    return {
        "active_count": active_count,
        "active_value": float(active_value),
        "returned_count": counts.get("RETURNED", 0),
        "unknown_count": counts.get("UNKNOWN", 0),
        "total_count": total_count,
        "avg_value": float(active_value / active_count) if active_count else 0.0,
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_churn_by_plan(
    organization: Organization, months: int = 3
) -> list[dict[str, Any]]:
    """Churn por plano nos últimos N meses.

    Para cada plan_name, conta contratos cancelados no período e calcula
    receita perdida (monthly_amount dos cancelados).

    Retorna lista ordenada por revenue_lost desc.
    """
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    canceled = (
        Contract.objects.filter(
            organization=organization,
            canceled_at__date__gte=cutoff,
        )
        .values("plan_name")
        .annotate(
            canceled=Count("id"),
            revenue_lost=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
        )
        .order_by("-revenue_lost")
    )

    # Base de ativos no início do período para calcular taxa
    base_counts = {
        row["contract__plan_name"]: row["n"]
        for row in FactContractStatusDaily.objects.filter(
            organization=organization,
            date=cutoff,
            is_active=True,
        )
        .values("contract__plan_name")
        .annotate(n=Count("id"))
    }

    result = []
    for row in canceled:
        plan = row["plan_name"] or "—"
        base = base_counts.get(plan, 0)
        churn_rate = float(row["canceled"] / base * 100) if base > 0 else 0.0
        result.append(
            {
                "plan": plan,
                "canceled": row["canceled"],
                "revenue_lost": float(row["revenue_lost"]),
                "base_start": base,
                "churn_rate": round(churn_rate, 1),
            }
        )
    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_blocked_duration_distribution(
    organization: Organization,
) -> list[dict[str, Any]]:
    """Distribuição de contratos BLOCKED por duração contínua do bloqueio.

    Para cada contrato BLOCKED hoje, calcula há quantos dias está no bloqueio
    atual (sem interrupção) comparando com o último dia em que NÃO era BLOCKED.

    Agrupa nos buckets: 1–7d · 8–15d · 16–30d · 31–60d · 60+ dias.
    """
    today = timezone.now().date()

    # Contratos BLOCKED hoje
    blocked_ids = list(
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, status="BLOCKED"
        ).values_list("contract_id", flat=True)
    )
    if not blocked_ids:
        return []

    # Último dia (antes de hoje) em que cada contrato NÃO era BLOCKED
    last_non_blocked = (
        FactContractStatusDaily.objects.filter(
            organization=organization,
            contract_id__in=blocked_ids,
            date__lt=today,
        )
        .exclude(status="BLOCKED")
        .values("contract_id")
        .annotate(last_date=Max("date"))
    )
    last_non_blocked_map = {row["contract_id"]: row["last_date"] for row in last_non_blocked}

    buckets: dict[str, dict] = {
        "1_7":    {"label": "1–7 dias",   "count": 0, "revenue": 0.0},
        "8_15":   {"label": "8–15 dias",  "count": 0, "revenue": 0.0},
        "16_30":  {"label": "16–30 dias", "count": 0, "revenue": 0.0},
        "31_60":  {"label": "31–60 dias", "count": 0, "revenue": 0.0},
        "over_60":{"label": "60+ dias",   "count": 0, "revenue": 0.0},
    }

    blocked_rows = FactContractStatusDaily.objects.filter(
        organization=organization, date=today, status="BLOCKED"
    ).values("contract_id", "monthly_amount")

    for row in blocked_rows:
        cid = row["contract_id"]
        last_ok = last_non_blocked_map.get(cid)
        if last_ok:
            days = (today - last_ok).days - 1
        else:
            days = 999  # bloqueado desde sempre → over_60

        days = max(1, days)
        rev = float(row["monthly_amount"] or 0)

        if days <= 7:
            b = "1_7"
        elif days <= 15:
            b = "8_15"
        elif days <= 30:
            b = "16_30"
        elif days <= 60:
            b = "31_60"
        else:
            b = "over_60"
        buckets[b]["count"] += 1
        buckets[b]["revenue"] += rev

    return [{"bucket": k, **v} for k, v in buckets.items()]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_blocked_at_risk_summary(
    organization: Organization, min_days: int = 30
) -> dict[str, Any]:
    """KPI — contratos BLOCKED há mais de min_days dias consecutivos."""
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()

    blocked_ids = list(
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, status="BLOCKED"
        ).values_list("contract_id", flat=True)
    )
    if not blocked_ids:
        return {"count": 0, "revenue_at_risk": 0.0, "pct_of_blocked": 0.0}

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
    ixc_blocked_since = {
        cid: _ixc_blocked_since(extras)
        for cid, extras in Contract.objects.filter(
            organization=organization, id__in=blocked_ids
        ).values_list("id", "raw_extras")
    }

    at_risk_ids = []
    for cid in blocked_ids:
        days = _days_blocked(today, last_non_blocked.get(cid), ixc_blocked_since.get(cid))
        if days >= min_days:
            at_risk_ids.append(cid)

    if not at_risk_ids:
        return {"count": 0, "revenue_at_risk": 0.0, "pct_of_blocked": 0.0}

    revenue_at_risk = float(
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, contract_id__in=at_risk_ids
        ).aggregate(s=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()))["s"] or 0
    )

    return {
        "count": len(at_risk_ids),
        "revenue_at_risk": revenue_at_risk,
        "pct_of_blocked": round(len(at_risk_ids) / len(blocked_ids) * 100, 1) if blocked_ids else 0.0,
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_at_risk_contracts(
    organization: Organization, min_days: int = 30, limit: int = 50
) -> list[dict[str, Any]]:
    """Contratos BLOCKED há mais de min_days dias — lista para ação de cobrança."""
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()

    blocked_ids = list(
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, status="BLOCKED"
        ).values_list("contract_id", flat=True)
    )
    if not blocked_ids:
        return []

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

    # Enriquecer com dados do Contract model
    contracts_map = {
        c.id: c
        for c in Contract.objects.filter(
            organization=organization, id__in=blocked_ids
        ).select_related("customer")
    }

    result = []
    for cid in blocked_ids:
        c = contracts_map.get(cid)
        if not c:
            continue
        last_ok = last_non_blocked.get(cid)
        ixc_since = _ixc_blocked_since(c.raw_extras)
        days = _days_blocked(today, last_ok, ixc_since)
        if days < min_days:
            continue
        start = _block_start_date(last_ok, ixc_since)
        blocked_since = start.isoformat() if start else "—"
        result.append(
            {
                "contract_id": cid,
                "contract_external_id": c.external_id,
                "customer_name": c.customer.name if c.customer else "—",
                "plan_name": c.plan_name or "—",
                "monthly_amount": float(c.monthly_amount),
                "blocked_since": blocked_since,
                "days_blocked": days,
            }
        )

    result.sort(key=lambda x: x["days_blocked"], reverse=True)
    return result[:limit]


# ---------------------------------------------------------------------------
# Mapeamento de motivos de cancelamento IXC → label legível + categoria
# ---------------------------------------------------------------------------
# Fonte: análise de obs_cancelamento nos contratos cancelados da base real.
# Categoria: True = controlável (ação de retenção possível)
#            False = não controlável (mobilidade, titularidade, etc.)
#            None = neutro/operacional
_IXC_MOTIVO_MAP: dict[str, tuple[str, bool | None]] = {
    "0":  ("Sem motivo registrado", None),
    "3":  ("Troca de titularidade", None),
    "4":  ("Mudança de titularidade", None),
    "5":  ("Mudança de endereço", False),
    "6":  ("Inadimplência acumulada", True),   # sistema cancela após bloqueio prolongado
    "8":  ("Outros", None),
    "9":  ("Fora da área de cobertura", False),
    "24": ("Desistência pré-instalação", None),
    "25": ("Trocou de provedor", True),
    "26": ("Cancelou serviço adicional", None),
    "27": ("Problemas de qualidade/suporte", True),
    "29": ("Contrato sem uso (operacional)", None),
    "30": ("Mudança de titularidade", None),
    "31": ("Mudança de cidade", False),
    "32": ("Saindo do local", False),
}


def _motivo_label(mid: str) -> str:
    return _IXC_MOTIVO_MAP.get(str(mid), (f"Motivo #{mid}", None))[0]


def _motivo_controlavel(mid: str) -> bool | None:
    return _IXC_MOTIVO_MAP.get(str(mid), (None, None))[1]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_mrr_churn_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Série mensal de MRR Churn + Logo Churn + MRR Recuperado.

    Para cada mês:
      - mrr_lost: soma das mensalidades dos contratos cancelados no mês
      - logo_churn: contagem de cancelamentos
      - mrr_recovered: soma das mensalidades de contratos ativados no mês
      - new_logos: contagem de ativações no mês
      - net_mrr: mrr_recovered - mrr_lost

    Retorna lista cronológica (mais antigo primeiro).
    """
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()
    result = []

    for i in range(months - 1, -1, -1):
        m_start = _first_of_month_n_ago(today, i)
        m_end = _first_of_month_n_ago(today, i - 1) - timedelta(days=1) if i > 0 else today
        label = m_start.strftime("%b/%y")
        month_key = m_start.strftime("%Y-%m")

        # Cancelamentos — apenas contratos que foram ativados (MRR real)
        # Pré-contratos (activated_at=NULL) nunca geraram receita, excluídos aqui
        canceled_agg = Contract.objects.filter(
            organization=organization,
            status="CANCELED",
            activated_at__isnull=False,
            canceled_at__date__gte=m_start,
            canceled_at__date__lte=m_end,
        ).aggregate(
            n=Count("id"),
            mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
        )

        # Ativações
        activated_agg = Contract.objects.filter(
            organization=organization,
            activated_at__date__gte=m_start,
            activated_at__date__lte=m_end,
        ).aggregate(
            n=Count("id"),
            mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
        )

        mrr_lost = float(canceled_agg["mrr"] or 0)
        mrr_recovered = float(activated_agg["mrr"] or 0)
        result.append({
            "month": month_key,
            "label": label,
            "mrr_lost": mrr_lost,
            "logo_churn": canceled_agg["n"] or 0,
            "mrr_recovered": mrr_recovered,
            "new_logos": activated_agg["n"] or 0,
            "net_mrr": mrr_recovered - mrr_lost,
        })

    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_churn_by_reason(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """MRR perdido por motivo de cancelamento (dos últimos N meses).

    Agrega por `motivo_cancelamento` do raw_extras. Retorna lista ordenada
    por mrr_lost desc, com label legível e categoria controlável/não.
    """
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    canceled = Contract.objects.filter(
        organization=organization,
        canceled_at__date__gte=cutoff,
    ).filter(_CANCELED_Q)

    reasons: dict[str, dict] = {}
    for c in canceled.iterator():
        mid = str((c.raw_extras or {}).get("motivo_cancelamento", "0") or "0")
        if mid not in reasons:
            reasons[mid] = {
                "motivo_id": mid,
                "label": _motivo_label(mid),
                "controlavel": _motivo_controlavel(mid),
                "count": 0,
                "mrr_lost": 0.0,
            }
        reasons[mid]["count"] += 1
        reasons[mid]["mrr_lost"] += float(c.monthly_amount or 0)

    result = sorted(reasons.values(), key=lambda x: -x["mrr_lost"])

    # Adicionar % acumulado (Pareto)
    total = sum(r["mrr_lost"] for r in result)
    acc = 0.0
    for r in result:
        acc += r["mrr_lost"]
        r["pct"] = round(r["mrr_lost"] / total * 100, 1) if total > 0 else 0.0
        r["pct_acc"] = round(acc / total * 100, 1) if total > 0 else 0.0

    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_ltv_distribution(
    organization: Organization,
) -> list[dict[str, Any]]:
    """Histograma de LTV — tempo de vida dos contratos cancelados.

    Buckets: <3 meses · 3–12 meses · 12–24 meses · 24+ meses.
    Retorna count + MRR médio por bucket.
    """
    from apps.customers.infrastructure.models import Contract

    canceled = Contract.objects.filter(
        organization=organization,
        canceled_at__isnull=False,
        activated_at__isnull=False,
    ).filter(_CANCELED_Q).iterator()

    buckets: dict[str, dict] = {
        "lt_3":   {"label": "< 3 meses",    "count": 0, "mrr_sum": 0.0},
        "3_12":   {"label": "3–12 meses",   "count": 0, "mrr_sum": 0.0},
        "12_24":  {"label": "12–24 meses",  "count": 0, "mrr_sum": 0.0},
        "over_24":{"label": "24+ meses",    "count": 0, "mrr_sum": 0.0},
    }

    for c in canceled:
        months = (c.canceled_at - c.activated_at).days / 30.4
        mrr = float(c.monthly_amount or 0)
        if months < 3:
            b = "lt_3"
        elif months < 12:
            b = "3_12"
        elif months < 24:
            b = "12_24"
        else:
            b = "over_24"
        buckets[b]["count"] += 1
        buckets[b]["mrr_sum"] += mrr

    result = []
    for key, data in buckets.items():
        avg_mrr = data["mrr_sum"] / data["count"] if data["count"] > 0 else 0.0
        result.append({
            "bucket": key,
            "label": data["label"],
            "count": data["count"],
            "avg_mrr": round(avg_mrr, 2),
        })
    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_churn_plan_detail(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Tabela detalhada de churn por plano (últimos N meses) com análise estatística.

    Métricas por plano:
      - count: cancelamentos absolutos no período
      - base: contratos ativos no início do período
      - churn_rate: count / base × 100  (taxa normalizada — remove viés de tamanho)
      - expected: base × overall_churn_rate / 100  (quantos esperaríamos cancelar)
      - excess: count − expected  (positivo = pior que a média, negativo = melhor)
      - risk_index: churn_rate / overall_rate  (1.0 = média, >1.5 = alto risco)
      - ltv_avg_months: tempo médio de vida dos contratos cancelados
      - mrr_lost: receita mensal perdida
      - pct_of_total: % do MRR churn total

    Retorna lista ordenada por risk_index desc (planos mais problemáticos primeiro)
    quando base > 0; demais ao final.
    """
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    canceled = Contract.objects.filter(
        organization=organization,
        canceled_at__date__gte=cutoff,
        activated_at__isnull=False,
    ).filter(_CANCELED_Q)

    # Agrupa por plano
    by_plan: dict[str, dict] = {}
    for c in canceled.iterator():
        plan = c.plan_name or "—"
        if plan not in by_plan:
            by_plan[plan] = {"count": 0, "mrr_lost": 0.0, "ltv_days": []}
        by_plan[plan]["count"] += 1
        by_plan[plan]["mrr_lost"] += float(c.monthly_amount or 0)
        days = (c.canceled_at - c.activated_at).days
        by_plan[plan]["ltv_days"].append(days)

    # Base de ativos no início do período (para churn rate normalizado)
    base_counts = {
        row["contract__plan_name"]: row["n"]
        for row in FactContractStatusDaily.objects.filter(
            organization=organization,
            date=cutoff,
            is_active=True,
        )
        .values("contract__plan_name")
        .annotate(n=Count("id"))
    }

    # Taxa global de churn no período: total cancelados / total base
    total_base = sum(base_counts.values())
    total_canceled = sum(v["count"] for v in by_plan.values())
    overall_rate = (total_canceled / total_base * 100) if total_base > 0 else 0.0

    total_mrr = sum(v["mrr_lost"] for v in by_plan.values())
    result = []
    for plan, data in by_plan.items():
        ltv_avg = sum(data["ltv_days"]) / len(data["ltv_days"]) / 30.4 if data["ltv_days"] else 0.0
        base = base_counts.get(plan, 0)
        if base > 0:
            churn_rate = round(data["count"] / base * 100, 1)
            expected = round(base * overall_rate / 100, 1)
            excess = round(data["count"] - expected, 1)
            risk_index = round(churn_rate / overall_rate, 2) if overall_rate > 0 else None
        else:
            churn_rate = None
            expected = None
            excess = None
            risk_index = None

        result.append({
            "plan": plan,
            "count": data["count"],
            "base": base,
            "mrr_lost": round(data["mrr_lost"], 2),
            "pct_of_total": round(data["mrr_lost"] / total_mrr * 100, 1) if total_mrr > 0 else 0.0,
            "ltv_avg_months": round(ltv_avg, 1),
            "churn_rate": churn_rate,
            "expected": expected,
            "excess": excess,
            "risk_index": risk_index,
        })

    # Ordena por risk_index (planos com churn acima da média primeiro); sem base ao final
    return sorted(
        result,
        key=lambda x: (-(x["risk_index"] or 0), -(x["count"])),
    )


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_churn_summary(organization: Organization) -> dict[str, Any]:
    """KPIs de churn para o topo do dashboard.

    Retorna:
      - mrr_lost_this_month: R$ perdido no mês corrente
      - logo_churn_this_month: contratos cancelados no mês
      - logo_churn_pct: logo_churn / ativos no início do mês
      - mrr_recovered_this_month: R$ de novos contratos ativados no mês
      - net_mrr_this_month: recovered - lost
      - ltv_avg_months: LTV médio de todos os cancelados com ativação
      - avg_ticket_canceled: ticket médio dos cancelados últimos 3m
      - avg_ticket_active: ticket médio dos contratos ativos hoje
    """
    from apps.customers.infrastructure.models import Contract
    from django.db.models import Avg

    today = timezone.now().date()
    month_first = today.replace(day=1)
    prev_month_first = _first_of_month_n_ago(today, 1)

    # Apenas contratos que foram ativados — pré-contratos abandonados não geram MRR.
    # _CANCELED_Q: defensivo contra UNKNOWN c/ canceled_at (bug histórico de re-sync).
    canceled_qs = Contract.objects.filter(
        organization=organization, activated_at__isnull=False
    ).filter(_CANCELED_Q)

    this_month = canceled_qs.filter(
        canceled_at__date__gte=month_first,
    ).aggregate(
        n=Count("id"),
        mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
    )

    activated_this = Contract.objects.filter(
        organization=organization,
        activated_at__date__gte=month_first,
    ).aggregate(
        n=Count("id"),
        mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()),
    )

    # Base ativa no início do mês
    base_start = FactContractStatusDaily.objects.filter(
        organization=organization,
        date=prev_month_first,
        is_active=True,
    ).count()

    logo_churn = this_month["n"] or 0
    mrr_lost = float(this_month["mrr"] or 0)
    mrr_recovered = float(activated_this["mrr"] or 0)

    # LTV médio
    ltv_data = canceled_qs.filter(
        canceled_at__isnull=False, activated_at__isnull=False
    ).extra(
        select={"days": "EXTRACT(DAY FROM (canceled_at - activated_at))"}
    )
    # Python-based avg (simpler cross-db)
    ltv_days_list = []
    for c in canceled_qs.filter(canceled_at__isnull=False, activated_at__isnull=False)[:2000].iterator():
        ltv_days_list.append((c.canceled_at - c.activated_at).days)
    ltv_avg_months = (sum(ltv_days_list) / len(ltv_days_list) / 30.4) if ltv_days_list else 0.0

    # Avg ticket comparison
    cutoff_3m = _first_of_month_n_ago(today, 3)
    avg_canceled = canceled_qs.filter(canceled_at__date__gte=cutoff_3m).aggregate(
        avg=Coalesce(Avg("monthly_amount"), _ZERO, output_field=DecimalField())
    )
    avg_active = Contract.objects.filter(
        organization=organization, status="ACTIVE"
    ).aggregate(avg=Coalesce(Avg("monthly_amount"), _ZERO, output_field=DecimalField()))

    avg_ticket_canceled = float(avg_canceled["avg"] or 0)
    avg_ticket_active = float(avg_active["avg"] or 0)

    return {
        "mrr_lost_this_month": mrr_lost,
        "logo_churn_this_month": logo_churn,
        "logo_churn_pct": round(logo_churn / base_start * 100, 2) if base_start > 0 else 0.0,
        "mrr_recovered_this_month": mrr_recovered,
        "new_logos_this_month": activated_this["n"] or 0,
        "net_mrr_this_month": mrr_recovered - mrr_lost,
        "ltv_avg_months": round(ltv_avg_months, 1),
        "avg_ticket_canceled": avg_ticket_canceled,
        "avg_ticket_active": avg_ticket_active,
        "ticket_alert": avg_ticket_canceled > avg_ticket_active,  # True = perdendo planos mais caros
    }


# ---------------------------------------------------------------------------
# Planejamento (plano de contas IXC) — mapa estático atualizado via API
# ---------------------------------------------------------------------------
# NOTA: O campo correto para classificação contábil em fn_apagar é `id_conta`
# (singular), que aponta para planejamento_analitico.id. O campo `id_contas`
# (plural) é a conta bancária (tabela `contas`) — NÃO usar para DRE.
#
# _CONTA_TO_PLANO: id_conta (planejamento_analitico.id) → id_planejamento (planejamento.id)
# Construído a partir do endpoint planejamento_analitico em 2026-05-29.
_CONTA_TO_PLANO: dict[str, str] = {
    "0":     "0",
    "103":   "73",   # Fretes
    "112":   "78",   # Despesas Comissões
    "115":   "9",    # Despesas Operacionais
    "141":   "56",   # CUSTO (Água)
    "147":   "10",   # Despesas Financeiras
    "174":   "52",   # Receitas Vendas Serviços
    "178":   "61",   # DESPESAS (Telefone)
    "181":   "68",   # Receitas Financeiras Juros
    "183":   "11",   # Veículos
    "187":   "10",   # Despesas Financeiras
    "212":   "94",   # SAÍDAS (INSS)
    "213":   "94",   # SAÍDAS (FGTS)
    "214":   "64",   # Custo com Impostos (Simples Nacional)
    "216":   "14",   # Despesas Não Operacionais
    "228":   "60",   # Despesas Operacionais (Aluguel Predial)
    "234":   "74",   # Serviços Terceiros (Honorários)
    "240":   "10",   # Despesas Financeiras (Taxas)
    "246":   "9",    # Despesas Operacionais
    "250":   "80",   # Custo Links e Transportes
    "263":   "14",   # Despesas Não Operacionais (Segurança)
    "267":   "88",   # Funcionários (Férias)
    "268":   "14",   # Despesas Não Operacionais (Viagem)
    "286":   "69",   # Receitas Financeiras Outros
    "324":   "9",    # Despesas Operacionais (serviços terceiros)
    "328":   "3",    # Fornecedores (Salários a pagar)
    "329":   "71",   # Cancelamentos
    "336":   "75",   # Custos das Mercadorias Vendidas
    "339":   "64",   # Custo com Impostos (IPI)
    "340":   "9",    # Despesas Operacionais (Acréscimos)
    "342":   "71",   # Cancelamentos (Renegociação)
    "345":   "61",   # DESPESAS (Energia Elétrica)
    "347":   "76",   # Despesas com Veículos (Combustível)
    "348":   "76",   # Despesas com Veículos (Manutenção)
    "353":   "76",   # Despesas com Veículos (IPVA)
    "354":   "64",   # Custo com Impostos (IPTU)
    "358":   "64",   # Custo com Impostos (ICMS)
    "359":   "10",   # Despesas Financeiras (Tarifas bancárias)
    "360":   "79",   # Publicidade (Facebook, Google)
    "367":   "3",    # Fornecedores
    "368":   "60",   # Despesas Operacionais (Serviços contábeis)
    "376":   "60",   # Despesas Operacionais (Manutenção e Limpeza)
    "380":   "32",   # Maquinas e Equipamentos
    "384":   "66",   # Despesas Financeiras (grupo)
    "395":   "72",   # Custos Sobre Operação
    "396":   "72",   # Custos Sobre Operação (Infra Terceiros)
    "397":   "8",    # Salários e ordenados
    "416":   "3",    # Fornecedores
    "417":   "74",   # Serviços Terceiros (Software)
    "422":   "9",    # Despesas Operacionais (Aluguel Infra)
    "432":   "64",   # Custo com Impostos
    "47":    "32",   # Maquinas e Equipamentos
    "549":   "3",    # Fornecedores (Anuidade Cartão)
    "551":   "3",    # Fornecedores
    "554":   "3",    # Fornecedores
    "565":   "3",    # Fornecedores (Compra HDD)
    "580":   "3",    # Fornecedores (Servidor)
    "586":   "3",    # Fornecedores (Limpeza)
    "602":   "3",    # Fornecedores (Uniformes)
    "608":   "3",    # Fornecedores (Vale Alimentação)
    "610":   "72",   # Custos Sobre Operação (Manutenção Contratos)
    "621":   "3",    # Fornecedores (Material)
    "637":   "56",   # CUSTO (Fornecedores)
    "772":   "3",    # Fornecedores (Curso técnico)
    "807":   "3",    # Fornecedores (Engenheiro)
    "879":   "3",    # Fornecedores
    "890":   "3",    # Fornecedores (Marketing Rede Social)
    "952":   "4",    # Clientes
    "1201":  "3",    # Fornecedores (ONU)
    "1875":  "3",    # Fornecedores (Tarifa Registro Cobrança)
    "2042":  "3",    # Fornecedores (Panfletagem)
    "2106":  "3",    # Fornecedores (Emprestimo Bradesco)
    "2507":  "3",    # Fornecedores (Ação de vendas)
    "2519":  "3",    # Fornecedores
    "2549":  "20",   # ATIVO CIRCULANTE (Consorcio)
    "2621":  "3",    # Fornecedores (Marketing diversos)
    "2669":  "3",    # Fornecedores
    "2715":  "88",   # Funcionários (Exames médicos)
    "2745":  "21",   # Caixa (Aporte)
    "2754":  "3",    # Fornecedores
    "3044":  "3",    # Fornecedores (Vale Transporte)
    "3118":  "3",    # Fornecedores
    "3144":  "3",    # Fornecedores (Limpeza ar cond)
    "3146":  "3",    # Fornecedores (IRPJ)
    "3148":  "3",    # Fornecedores (CSLL)
    "3291":  "59",   # Custos dos Serviços Vendidos
    "4050":  "12",   # Empréstimos e financiamentos (Bancário)
    "4064":  "59",   # Custos dos Serviços Vendidos (Materiais Instalação)
    "4407":  "3",    # Fornecedores (Correios)
    "4408":  "3",    # Fornecedores
    "5895":  "94",   # SAÍDAS (Vale Transporte)
    "5927":  "56",   # CUSTO (Vale Alimentação)
    "6055":  "60",   # Despesas Operacionais (Móveis)
    "6056":  "60",   # Despesas Operacionais (Supermercado)
    "6058":  "60",   # Despesas Operacionais (Limpeza e Higiene)
    "6059":  "63",   # Folha de Pagamento (Holerite)
    "6087":  "59",   # Custos dos Serviços Vendidos (TV)
    "6584":  "33",   # Participações Societárias (Retirada Lucro)
    "6608":  "60",   # Despesas Operacionais (Construção)
    "6632":  "93",   # Pro-Labore
    "6638":  "92",   # Honorários Advocatícios
    "6643":  "60",   # Despesas Operacionais (Materiais Escritório)
    "6765":  "72",   # Custos Sobre Operação (13º)
    "6879":  "22",   # Bancos (Anuidade cartão crédito)
    "6980":  "72",   # Custos Sobre Operação (Locação Infra)
    "7114":  "51",   # Receitas Vendas Telefonia
    "7203":  "88",   # Funcionários (Uniformes)
    "7266":  "79",   # Publicidade (Brindes)
    "7296":  "66",   # Despesas Financeiras (Rescisórias)
    "10028": "27",   # INVESTIMENTOS
    "10168": "12",   # Empréstimos e financiamentos
    "10459": "76",   # Despesas com Veículos (Seguro)
    "10703": "59",   # Custos dos Serviços Vendidos (Manutenção)
    "10912": "61",   # DESPESAS (Manutenção predial)
    "11196": "61",   # DESPESAS
    "11244": "27",   # INVESTIMENTOS
    "11285": "27",   # INVESTIMENTOS (Comissão)
    "12011": "61",   # DESPESAS (Ferramentas)
}

_PLANEJAMENTO: dict[str, dict[str, str]] = {
    "1":  {"cod": "1.1.01.001",       "nome": "Caixa Geral",                         "tipo": "A"},
    "2":  {"cod": "1.1.02.001",       "nome": "Bancos Geral",                         "tipo": "A"},
    "3":  {"cod": "2.1.02.001",       "nome": "Fornecedores",                         "tipo": "P"},
    "4":  {"cod": "1.1.03.001",       "nome": "Clientes",                             "tipo": "A"},
    "5":  {"cod": "3.1.01.001",       "nome": "Receitas Vendas Internet",             "tipo": "R"},
    "6":  {"cod": "3.2.01.001",       "nome": "Receitas Financeiras Descontos",       "tipo": "R"},
    "7":  {"cod": "1.1.04.001",       "nome": "Estoque Geral",                        "tipo": "A"},
    "8":  {"cod": "5.2.02.001",       "nome": "Salários e ordenados",                 "tipo": "D"},
    "9":  {"cod": "5.2.01.002",       "nome": "Despesas Operacionais",                "tipo": "D"},
    "10": {"cod": "5.3.01.001",       "nome": "Despesas Financeiras",                 "tipo": "D"},
    "11": {"cod": "1.2.02.004",       "nome": "Veículos",                             "tipo": "A"},
    "12": {"cod": "5.3.01.0001",      "nome": "Empréstimos e financiamentos",         "tipo": "D"},  # cod corrigido p/ 5.3 (financeiro)
    "13": {"cod": "2.3.01.001",       "nome": "Dividendos",                           "tipo": "P"},
    "14": {"cod": "5.3.02.001",       "nome": "Outras Despesas Não Operacionais",     "tipo": "D"},
    "15": {"cod": "1.2.01.002",       "nome": "Aplicações",                           "tipo": "A"},
    "16": {"cod": "1.1.05.001",       "nome": "Adiantamento a fornecedores",          "tipo": "A"},
    "17": {"cod": "2.1.02.003",       "nome": "Adiantamento de clientes",             "tipo": "P"},
    "18": {"cod": "1.1.03.002",       "nome": "Títulos em cartório",                  "tipo": "A"},
    "56": {"cod": "4",                "nome": "CUSTO",                                "tipo": "C"},
    "57": {"cod": "4.1",              "nome": "Custos de Vendas",                     "tipo": "C"},
    "58": {"cod": "4.1.01",           "nome": "Custos dos Produtos Vendidos",         "tipo": "C"},
    "59": {"cod": "4.1.02",           "nome": "Custos dos Serviços Vendidos",         "tipo": "C"},
    "60": {"cod": "5.2",              "nome": "Despesas Operacionais (grupo)",        "tipo": "D"},
    "61": {"cod": "5",                "nome": "DESPESAS",                             "tipo": "D"},
    "62": {"cod": "5.2.02.002.0001",  "nome": "Aluguéis",                            "tipo": "D"},
    "63": {"cod": "5.2.02",           "nome": "Folha de Pagamento",                   "tipo": "D"},
    "64": {"cod": "4.2.01.003",       "nome": "Custo com Impostos",                   "tipo": "C"},
    "65": {"cod": "5.3",              "nome": "Despesas Não Operacionais",             "tipo": "D"},
    "66": {"cod": "5.3.01",           "nome": "Despesas Financeiras (grupo)",         "tipo": "D"},
    "67": {"cod": "5.3.02",           "nome": "Outras Despesas Não Op (grupo)",       "tipo": "D"},
    "68": {"cod": "3.2.01.002",       "nome": "Receitas Financeiras Juros",           "tipo": "R"},
    "69": {"cod": "3.2.01.003",       "nome": "Receitas Financeiras Outros",          "tipo": "R"},
    "70": {"cod": "5.2.01.004",       "nome": "Uso e Consumo",                        "tipo": "D"},
    "71": {"cod": "5.2.01.005",       "nome": "Cancelamentos",                        "tipo": "D"},
    "72": {"cod": "4.2.02.002",       "nome": "Custos Sobre Operação",                "tipo": "C"},
    "73": {"cod": "5.2.01.006",       "nome": "Fretes",                               "tipo": "D"},
    "74": {"cod": "5.2.01.007",       "nome": "Serviços Terceiros",                   "tipo": "D"},
    "75": {"cod": "4.1.01.001",       "nome": "Custos das Mercadorias Vendidas",      "tipo": "C"},
    "76": {"cod": "5.2.01.008",       "nome": "Despesas com Veículos",                "tipo": "D"},
    "77": {"cod": "5.1",              "nome": "Despesas Comerciais",                  "tipo": "D"},
    "78": {"cod": "5.1.03.001",       "nome": "Despesas Comissões",                   "tipo": "D"},
    "79": {"cod": "5.1.03.002",       "nome": "Publicidade",                          "tipo": "D"},
    "80": {"cod": "4.1.02.001",       "nome": "Custo Links e Transportes",            "tipo": "C"},
    "20": {"cod": "1.1",              "nome": "Ativo Circulante",                     "tipo": "A"},
    "21": {"cod": "1.1.01",           "nome": "Caixa",                                "tipo": "A"},
    "22": {"cod": "1.1.02",           "nome": "Bancos",                               "tipo": "A"},
    "27": {"cod": "1.2.01",           "nome": "Investimentos",                        "tipo": "A"},
    "32": {"cod": "1.2.02.003",       "nome": "Máquinas e Equipamentos",              "tipo": "A"},
    "33": {"cod": "1.2.01.001",       "nome": "Participações Societárias",            "tipo": "A"},
    "51": {"cod": "3.1.01.002",       "nome": "Receitas Vendas Telefonia",            "tipo": "R"},
    "52": {"cod": "3.1.01.003",       "nome": "Receitas Vendas Serviços",             "tipo": "R"},
    "82": {"cod": "1.2.02.005",       "nome": "Comodatos",                            "tipo": "A"},
    "86": {"cod": "5.2.02.003",       "nome": "Férias e Décimos",                     "tipo": "D"},
    "87": {"cod": "5.2.02.004",       "nome": "Admissões e Rescisões",                "tipo": "D"},
    "88": {"cod": "5.4",              "nome": "Funcionários (despesas geral)",        "tipo": "D"},
    "92": {"cod": "5.3.02.002",       "nome": "Honorários Advocatícios",              "tipo": "D"},
    "93": {"cod": "4.2.",             "nome": "Pro-Labore",                           "tipo": "C"},
    "94": {"cod": "5.2.02.001",       "nome": "Encargos Sociais",                     "tipo": "D"},  # INSS/FGTS/VT
    "95": {"cod": "1.2.01.03",        "nome": "Comissão (investimento)",              "tipo": "A"},
    "0":  {"cod": "",                 "nome": "(Sem categoria)",                      "tipo": "?"},
}

# Mapeamento: prefixo do cod contábil → (nome da seção DRE, ordem)
# Dois segmentos têm prioridade sobre um segmento.
_DRE_SECTION_MAP: dict[str, tuple[str, int]] = {
    "5.1": ("Despesas Comerciais",              2),
    "5.2": ("Despesas Operacionais",            3),
    "5.3": ("Despesas Financeiras",             4),
    "5.4": ("Outras Despesas",                  5),
    # Fallbacks single-segment
    "4":   ("Custos dos Serviços",              1),
    "5":   ("Outras Despesas",                  5),
    "2":   ("Despesas Gerais (A Classificar)",  6),
    "3":   ("Outros Lançamentos",               7),
    "1.2": ("Investimentos & Imobilizado",      8),
    "1.1": ("Movimentações de Caixa",           9),
    "1":   ("Movimentações de Ativo",           9),
}

# Overrides por cod COMPLETO: contas cuja classificação por prefixo no plano
# IXC não reflete a natureza real do lançamento. Têm prioridade sobre o
# mapa de prefixos em `_get_dre_section`.
#   - "5.1.03.0001" Empréstimos e financiamentos: o IXC pendura sob 5.1
#     (Comerciais), mas são parcelas de empréstimo/financiamento → Financeiras.
#     (vide #39: parcelamentos dominam o fluxo de caixa programado)
_DRE_ACCOUNT_OVERRIDES: dict[str, tuple[str, int]] = {
    "5.1.03.0001": ("Despesas Financeiras", 4),
}

# Fornecedores que são pessoas físicas ou PJ individuais (não empresas grandes)
# Mapeados a partir do endpoint `fornecedor` da API IXC.
# Formato: supplier_external_id → (nome_display, tipo)
# tipo: "PJ" = prestador PJ, "PF" = pessoa física, "SOCIO" = sócio
_PERSON_SUPPLIERS: dict[str, tuple[str, str]] = {
    "183": ("LUANN CARDOSO MARINS",           "PJ"),
    "181": ("PAULO SÓCIO",                    "SÓCIO"),
    "324": ("VINICIUS DALAS CORDEIRO",        "PJ"),
    "360": ("ANA CAROLINA TRINDADE",          "PJ"),
    "98":  ("GISLAINE MOREIRA INACIO",        "PJ"),
    "326": ("MARCO AURÉLIO SCHIAVON",         "PJ"),
    "383": ("KAINAN MOREIRA SILVA",           "PJ"),
    "436": ("GUILHERME FERNANDES DE LIMA",    "PJ"),
    "472": ("GUILHERME AUGUSTO PATRIAN",      "PJ"),
    "443": ("GIOVANNA CORDEIRO",              "PJ"),
    "386": ("GIOVANA IMPERATRICE NANNI",      "PJ"),
    "526": ("GILBERTO CRUZEIRO",              "PJ"),
    "177": ("JENIFER VITORIA DE SOUZA SANTOS", "PJ"),
    "207": ("MARIA CAROLINE LOPES",           "PJ"),
    "208": ("MARCOS ANDRE PROENÇA DE MORAIS", "PJ"),
    "215": ("GABRIELLA DE MOURA HARDT",       "PJ"),
    "217": ("BRUNA PRISCILA ANDRADE DA SILVA", "PJ"),
    "224": ("MARIA CLAUDIA FAUSTINO CALDEIRA", "PJ"),
    "325": ("MAYARA ARAÚJO ROBERTO",          "PJ"),
    "480": ("ERICK EDUARDO FERREIRA",         "PJ"),
    "482": ("RODRIGO RIBEIRO DOBBINS",        "PJ"),
    "484": ("ALEXANDRE MACHADO NEGRAO",       "PJ"),
    "485": ("GABRIELA VASARI NUNES",          "PJ"),
    # Categorias coletivas
    "205": ("TERCEIROS",                      "COLETIVO"),
    "85":  ("JOSE WILLIAN",                   "PJ"),
    "5":   ("ENGENHEIRO",                     "PJ"),
    "38":  ("ELETRICISTA",                    "PJ"),
    "39":  ("ELETRICISTA",                    "PJ"),
}

# IDs de fornecedores de MÃO DE OBRA (buscar por nome pois pode ser supplier_name)
_MAO_DE_OBRA_NAME = "MÃO DE OBRA TERCERIZADA"


def _load_plano_maps(organization: "Organization") -> "tuple[dict, dict]":
    """Carrega (conta_map, plano_map) do PlanoContasCache do banco.

    conta_map: {id_conta (str) → id_planejamento (str)}
    plano_map: {id_planejamento (str) → {cod, nome, tipo}}

    Fallback automático para os dicts hardcoded se a org ainda não tiver
    sido sincronizada via `python manage.py sync_planejamento <slug>`.
    """
    try:
        from apps.analytics.infrastructure.models import PlanoContasCache
        cache = PlanoContasCache.objects.get(organization=organization)
        if cache.conta_map and cache.plano_map:
            return cache.conta_map, cache.plano_map
    except Exception:
        pass
    return _CONTA_TO_PLANO, _PLANEJAMENTO


def _load_supplier_map(organization: "Organization") -> dict[str, str]:
    """Carrega {id_fornecedor → nome} do FornecedorCache do banco.

    Retorna dict vazio se a org ainda não tiver sido sincronizada via
    `python manage.py sync_fornecedores <slug>`.
    """
    try:
        from apps.analytics.infrastructure.models import FornecedorCache
        cache = FornecedorCache.objects.get(organization=organization)
        return cache.supplier_map or {}
    except Exception:
        return {}


def _resolve_supplier_name(
    supplier_external_id: str | None,
    stored_name: str | None,
    supplier_map: dict[str, str],
) -> str:
    """Resolve o nome de exibição de um fornecedor.

    Prioriza o nome do FornecedorCache (mais atual) — corrige inclusive
    despesas gravadas com o fallback antigo `Fornecedor #X`. Cai para o
    `supplier_name` gravado e, por fim, para `(Sem fornecedor)`.
    """
    sid = str(supplier_external_id or "").strip()
    if sid and sid != "0":
        cached = (supplier_map.get(sid) or "").strip()
        if cached and not cached.startswith("Fornecedor #"):
            return cached
    stored = (stored_name or "").strip()
    if stored:
        return stored
    return "(Sem fornecedor)"


def _resolve_conta(
    id_conta: str | None,
    conta_map: dict | None = None,
    plano_map: dict | None = None,
) -> dict[str, str]:
    """Dado o id_conta (planejamento_analitico.id), retorna o planejamento pai.

    Fluxo: id_conta → conta_map → id_planejamento → plano_map → {cod, nome, tipo}
    Usa dicts hardcoded como fallback se conta_map/plano_map não fornecidos.
    """
    cm = conta_map if conta_map is not None else _CONTA_TO_PLANO
    pm = plano_map if plano_map is not None else _PLANEJAMENTO
    ic = str(id_conta or "0").strip()
    id_plan = cm.get(ic, "0")
    return pm.get(id_plan) or pm.get("0") or {"cod": "", "nome": f"Conta #{ic}", "tipo": "?"}


def _get_planeja_label(
    id_conta: str | None,
    conta_map: dict | None = None,
    plano_map: dict | None = None,
) -> str:
    """Retorna label do planejamento pai: 'cod — nome' ou fallback.

    Usa id_conta (planejamento_analitico.id) para resolver via conta_map.
    """
    entry = _resolve_conta(id_conta, conta_map=conta_map, plano_map=plano_map)
    return f"{entry['cod']} {entry['nome']}".strip() or "(Sem categoria)"


def _get_dre_section(cod: str) -> tuple[str, int]:
    """Mapeia cod contábil para (seção DRE, ordem).

    Primeiro consulta overrides por cod completo (`_DRE_ACCOUNT_OVERRIDES`);
    depois verifica prefixo de dois segmentos antes de um — ex: "5.2" tem
    prioridade sobre "5". Usado para montar a estrutura de linhas da DRE.
    """
    if not cod:
        return ("Sem Categoria", 99)
    cod_clean = cod.strip().rstrip(".")
    override = _DRE_ACCOUNT_OVERRIDES.get(cod_clean)
    if override:
        return override
    parts = cod_clean.split(".")
    top = parts[0]
    two_prefix = f"{top}.{parts[1]}" if len(parts) > 1 else ""
    entry = _DRE_SECTION_MAP.get(two_prefix) or _DRE_SECTION_MAP.get(top)
    return entry or ("Sem Categoria", 99)


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_dre_by_account(
    organization: Organization,
    months: int = 12,
    from_ym: str | None = None,
    to_ym: str | None = None,
) -> dict[str, Any]:
    """DRE estruturado por plano de contas IXC.

    Suporta janela relativa (months=N, padrão) ou janela fixa via from_ym/to_ym
    (strings no formato "YYYY-MM").

    Retorna:
      - months / month_labels: lista de períodos no intervalo
      - categories: lista flat de contas com {id, cod, nome, label, tipo,
          section, section_order, monthly, total}
      - sections: contas agrupadas por seção DRE
          [{section, order, monthly, total, accounts}]
      - dre_rows: linhas estruturadas da DRE para tabela P&L:
          tipo "header" | "section" | "subtotal" | "total"
      - revenue_series: [{month, label, mrr}] alinhado ao intervalo
      - summary: {total_expenses, total_revenue, ebitda}
    """
    import calendar
    from apps.financial.infrastructure.models import Expense as ExpenseModel

    conta_map, plano_map = _load_plano_maps(organization)
    supplier_map = _load_supplier_map(organization)
    today = timezone.now().date()

    # --- Intervalo de datas ---
    if from_ym and to_ym:
        try:
            cutoff = date_cls.fromisoformat(from_ym + "-01")
            end_ym = date_cls.fromisoformat(to_ym + "-01")
            _, last_day = calendar.monthrange(end_ym.year, end_ym.month)
            end_date = end_ym.replace(day=last_day)
        except ValueError:
            from_ym = to_ym = None

    if not (from_ym and to_ym):
        cutoff = _first_of_month_n_ago(today, months)
        end_date = today

    # Quantos meses de histórico precisamos para MRR
    months_back_for_mrr = max(
        months,
        (today.year - cutoff.year) * 12 + (today.month - cutoff.month) + 2,
    )

    # --- Query 1: despesas agrupadas por (mês, id_conta) ---
    # NOTA: usa id_conta (singular) = planejamento_analitico.id (conta analítica)
    # NÃO usa id_contas (plural) = contas.id (conta bancária de pagamento)
    _base_filter = dict(
        organization=organization,
        status="PAID",
        paid_at__isnull=False,
        paid_at__gte=cutoff,
        paid_at__lte=end_date,
    )
    qs = (
        ExpenseModel.objects.filter(**_base_filter)
        .annotate(
            month=TruncMonth("paid_at"),
            id_conta_str=KeyTextTransform("id_conta", "raw_extras"),
        )
        .values("month", "id_conta_str")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
        .order_by("month", "id_conta_str")
    )

    # Monta mapa: id_conta_analitica → {YYYY-MM → valor}
    # Depois agrupa por id_planejamento (conta pai) via conta_map (do PlanoContasCache)
    all_months_set: set[str] = set()
    raw_analitico: dict[str, dict[str, float]] = {}  # id_conta → {YYYY-MM → valor}
    for row in qs:
        mk = row["month"].strftime("%Y-%m")
        all_months_set.add(mk)
        ic = str(row["id_conta_str"] or "0")
        raw_analitico.setdefault(ic, {})[mk] = float(row["total"])

    # Agrega por id_planejamento (conta pai)
    raw: dict[str, dict[str, float]] = {}  # id_planejamento → {YYYY-MM → valor}
    for ic, monthly_data in raw_analitico.items():
        id_plan = conta_map.get(ic, "0")
        for mk, val in monthly_data.items():
            raw.setdefault(id_plan, {})[mk] = raw.get(id_plan, {}).get(mk, 0.0) + val

    # --- Query 2: breakdown por fornecedor dentro de cada conta ---
    qs_sup = (
        ExpenseModel.objects.filter(**_base_filter)
        .annotate(
            month=TruncMonth("paid_at"),
            id_conta_str=KeyTextTransform("id_conta", "raw_extras"),
        )
        .values("month", "id_conta_str", "supplier_external_id", "supplier_name")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
        .order_by("id_conta_str", "supplier_name", "month")
    )
    # {id_planejamento → {supplier → {YYYY-MM → float}}} — agrega por conta pai
    supplier_raw: dict[str, dict[str, dict[str, float]]] = {}
    for row in qs_sup:
        ic = str(row["id_conta_str"] or "0")
        id_plan = conta_map.get(ic, "0")
        sup = _resolve_supplier_name(
            row["supplier_external_id"], row["supplier_name"], supplier_map
        )
        mk = row["month"].strftime("%Y-%m")
        supplier_raw.setdefault(id_plan, {}).setdefault(sup, {})
        supplier_raw[id_plan][sup][mk] = supplier_raw[id_plan][sup].get(mk, 0.0) + float(row["total"])

    sorted_months = sorted(all_months_set)
    month_labels = []
    for mk in sorted_months:
        try:
            month_labels.append(date_cls.fromisoformat(mk + "-01").strftime("%b/%y"))
        except ValueError:
            month_labels.append(mk)

    n = len(sorted_months)

    # --- Categorias (flat) com info de seção DRE + breakdown por fornecedor ---
    # raw agora está keyed por id_planejamento (conta pai)
    categories: list[dict[str, Any]] = []
    for id_plan, monthly_data in sorted(raw.items(), key=lambda kv: -sum(kv[1].values())):
        entry = plano_map.get(id_plan, {})
        cod = entry.get("cod", "")
        section_name, section_order = _get_dre_section(cod)
        amounts = [monthly_data.get(mk, 0.0) for mk in sorted_months]
        total = sum(amounts)

        # Fornecedores dentro desta conta, ordenados por total desc
        suppliers: list[dict[str, Any]] = []
        for sup_name, sup_monthly in sorted(
            supplier_raw.get(id_plan, {}).items(),
            key=lambda kv: -sum(kv[1].values()),
        ):
            sup_amounts = [sup_monthly.get(mk, 0.0) for mk in sorted_months]
            sup_total = sum(sup_amounts)
            if sup_total > 0:
                suppliers.append({
                    "name": sup_name,
                    "monthly": sup_amounts,
                    "total": sup_total,
                })

        categories.append({
            "id": id_plan,
            "cod": cod,
            "nome": entry.get("nome", f"Conta #{id_plan}"),
            "label": entry.get("nome", f"Conta #{id_plan}"),
            "tipo": entry.get("tipo", "?"),
            "section": section_name,
            "section_order": section_order,
            "monthly": amounts,
            "total": total,
            "suppliers": suppliers,
        })

    # --- Agrupamento por seção DRE ---
    sections_map: dict[str, dict[str, Any]] = {}
    for cat in categories:
        sname = cat["section"]
        if sname not in sections_map:
            sections_map[sname] = {
                "section": sname,
                "order": cat["section_order"],
                "monthly": [0.0] * n,
                "total": 0.0,
                "accounts": [],
            }
        for i, amt in enumerate(cat["monthly"]):
            sections_map[sname]["monthly"][i] += amt
        sections_map[sname]["total"] += cat["total"]
        sections_map[sname]["accounts"].append(cat)

    sections = sorted(sections_map.values(), key=lambda s: s["order"])

    # --- Receita (MRR) alinhada ao intervalo ---
    rev_full = compute_mrr_series(organization, months=months_back_for_mrr)
    rev_by_month: dict[str, float] = {r["month"]: float(r["mrr"]) for r in rev_full}
    revenue_monthly = [rev_by_month.get(mk, 0.0) for mk in sorted_months]
    revenue_total = sum(revenue_monthly)

    # --- Linhas DRE estruturadas (P&L) ---
    _COST_SECTIONS = {"Custos dos Serviços"}
    _OPEX_SECTIONS = {"Despesas Comerciais", "Despesas Operacionais"}

    def _sub(a: list[float], b: list[float]) -> list[float]:
        return [x - y for x, y in zip(a, b)]

    dre_rows: list[dict[str, Any]] = []
    dre_rows.append({
        "type": "header",
        "label": "(+) Receita Bruta (MRR)",
        "monthly": list(revenue_monthly),
        "total": revenue_total,
    })

    # Custos → Resultado Bruto
    resultado_bruto = list(revenue_monthly)
    for sec in sections:
        if sec["section"] in _COST_SECTIONS:
            dre_rows.append({
                "type": "section", "sign": "(-)",
                "label": sec["section"],
                "monthly": sec["monthly"], "total": sec["total"],
                "accounts": sec["accounts"],
            })
            resultado_bruto = _sub(resultado_bruto, sec["monthly"])
    dre_rows.append({
        "type": "subtotal", "label": "Resultado Bruto",
        "monthly": resultado_bruto, "total": sum(resultado_bruto),
    })

    # Opex → EBITDA Operacional
    ebitda_monthly = list(resultado_bruto)
    for sec in sections:
        if sec["section"] in _OPEX_SECTIONS:
            dre_rows.append({
                "type": "section", "sign": "(-)",
                "label": sec["section"],
                "monthly": sec["monthly"], "total": sec["total"],
                "accounts": sec["accounts"],
            })
            ebitda_monthly = _sub(ebitda_monthly, sec["monthly"])
    dre_rows.append({
        "type": "subtotal", "label": "EBITDA Operacional",
        "monthly": ebitda_monthly, "total": sum(ebitda_monthly),
    })

    # Demais seções → Resultado Líquido
    resultado_liq = list(ebitda_monthly)
    for sec in sections:
        if sec["section"] not in _COST_SECTIONS and sec["section"] not in _OPEX_SECTIONS:
            dre_rows.append({
                "type": "section", "sign": "(-)",
                "label": sec["section"],
                "monthly": sec["monthly"], "total": sec["total"],
                "accounts": sec["accounts"],
            })
            resultado_liq = _sub(resultado_liq, sec["monthly"])
    dre_rows.append({
        "type": "total", "label": "Resultado Líquido",
        "monthly": resultado_liq, "total": sum(resultado_liq),
    })

    total_expenses = sum(s["total"] for s in sections)
    ebitda = revenue_total - total_expenses

    return {
        "months": sorted_months,
        "month_labels": month_labels,
        "categories": categories,
        "sections": sections,
        "dre_rows": dre_rows,
        "revenue_series": [
            {"month": mk, "label": ml, "mrr": rv}
            for mk, ml, rv in zip(sorted_months, month_labels, revenue_monthly)
        ],
        "summary": {
            "total_expenses": total_expenses,
            "total_revenue": revenue_total,
            "ebitda": ebitda,
        },
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_people_expenses(
    organization: Organization, months: int = 12
) -> dict[str, Any]:
    """Despesas com pessoas: prestadores PJ, sócios e coletivos por mês.

    Inclui:
    - Fornecedores individuais mapeados em _PERSON_SUPPLIERS
    - MÃO DE OBRA TERCERIZADA (supplier_name match)

    Retorna:
      - months: lista de rótulos ['2024-01', ...]
      - month_labels: ['Jan/24', ...]
      - people: list[{name, tipo, monthly: [float], total: float}]
      - mao_de_obra: {monthly: [float], total: float}
      - grand_total_monthly: [float]
      - grand_total: float
    """
    from apps.financial.infrastructure.models import Expense as ExpenseModel

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    # Query person suppliers
    person_ids = list(_PERSON_SUPPLIERS.keys())
    qs_people = (
        ExpenseModel.objects.filter(
            organization=organization,
            status="PAID",
            paid_at__isnull=False,
            paid_at__gte=cutoff,
            supplier_external_id__in=person_ids,
        )
        .annotate(month=TruncMonth("paid_at"))
        .values("month", "supplier_external_id", "supplier_name")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
        .order_by("month")
    )

    # Query MÃO DE OBRA TERCERIZADA separately (by name)
    qs_mao = (
        ExpenseModel.objects.filter(
            organization=organization,
            status="PAID",
            paid_at__isnull=False,
            paid_at__gte=cutoff,
            supplier_name=_MAO_DE_OBRA_NAME,
        )
        .annotate(month=TruncMonth("paid_at"))
        .values("month")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
        .order_by("month")
    )

    # Collect all months
    all_months_set: set[str] = set()
    # person_data: {supplier_external_id → {month_key → amount}}
    person_data: dict[str, dict[str, float]] = {}
    for row in qs_people:
        mk = row["month"].strftime("%Y-%m")
        all_months_set.add(mk)
        sid = row["supplier_external_id"]
        person_data.setdefault(sid, {})[mk] = person_data.get(sid, {}).get(mk, 0.0) + float(row["total"])

    mao_data: dict[str, float] = {}
    for row in qs_mao:
        mk = row["month"].strftime("%Y-%m")
        all_months_set.add(mk)
        mao_data[mk] = float(row["total"])

    sorted_months = sorted(all_months_set)
    month_labels = []
    for mk in sorted_months:
        try:
            from datetime import date as _d
            month_labels.append(_d.fromisoformat(mk + "-01").strftime("%b/%y"))
        except ValueError:
            month_labels.append(mk)

    # Build people list
    people = []
    for sid, meta in _PERSON_SUPPLIERS.items():
        if sid not in person_data:
            continue
        name, tipo = meta
        monthly_map = person_data[sid]
        amounts = [monthly_map.get(mk, 0.0) for mk in sorted_months]
        total = sum(amounts)
        if total > 0:
            people.append({
                "id": sid,
                "name": name,
                "tipo": tipo,
                "monthly": amounts,
                "total": total,
            })
    people.sort(key=lambda p: -p["total"])

    # MÃO DE OBRA
    mao_amounts = [mao_data.get(mk, 0.0) for mk in sorted_months]
    mao_total = sum(mao_amounts)

    # Grand total per month
    grand_monthly = [0.0] * len(sorted_months)
    for p in people:
        for i, amt in enumerate(p["monthly"]):
            grand_monthly[i] += amt
    for i, amt in enumerate(mao_amounts):
        grand_monthly[i] += amt

    return {
        "months": sorted_months,
        "month_labels": month_labels,
        "people": people,
        "mao_de_obra": {
            "name": _MAO_DE_OBRA_NAME,
            "tipo": "COLETIVO",
            "monthly": mao_amounts,
            "total": mao_total,
        },
        "grand_total_monthly": grand_monthly,
        "grand_total": sum(grand_monthly),
    }


def _normalize_mao_label(description: str) -> str:
    """Extrai rótulo legível da primeira linha da descrição de Mão de Obra."""
    import re as _re
    if not description:
        return "(Sem descrição)"
    first = description.split("\r\n")[0].split("\n")[0].strip()
    # colapsa espaços duplos
    first = _re.sub(r"\s{2,}", " ", first).strip()
    if not first:
        return "(Sem descrição)"
    return first[:80]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_mao_de_obra_detail(
    organization: Organization, months: int = 12
) -> dict[str, Any]:
    """Detalhamento de pagamentos de MÃO DE OBRA TERCERIZADA no período.

    Retorna:
      - transactions: list[{paid_at_str, amount, amount_str, label}]
        — todos os registros do período, mais recentes primeiro
      - by_category: list[{label, count, total, total_str, pct, monthly: [float]}]
        — agrupado por primeira linha da descrição, ordenado por total desc
      - month_labels: list[str]  — para o gráfico
      - months: list[str]        — YYYY-MM
      - total: float
      - count: int
    """
    from apps.financial.infrastructure.models import Expense as ExpenseModel

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    qs = (
        ExpenseModel.objects.filter(
            organization=organization,
            supplier_name=_MAO_DE_OBRA_NAME,
            status="PAID",
            paid_at__isnull=False,
            paid_at__gte=cutoff,
        )
        .order_by("-paid_at")
        .values("paid_at", "amount", "description")
    )

    transactions = []
    # {label → {month_key → float}}
    cat_monthly: dict[str, dict[str, float]] = {}
    cat_counts: dict[str, int] = {}
    all_months_set: set[str] = set()

    for row in qs:
        amt = float(row["amount"])
        label = _normalize_mao_label(row["description"] or "")
        paid_date = row["paid_at"]
        mk = paid_date.strftime("%Y-%m") if paid_date else ""
        paid_str = paid_date.strftime("%d/%m/%Y") if paid_date else "—"

        transactions.append({
            "paid_at_str": paid_str,
            "amount": amt,
            "amount_str": f"R$ {amt:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
            "label": label,
        })

        if mk:
            all_months_set.add(mk)
        if label not in cat_monthly:
            cat_monthly[label] = {}
            cat_counts[label] = 0
        if mk:
            cat_monthly[label][mk] = cat_monthly[label].get(mk, 0.0) + amt
        cat_counts[label] += 1

    # Padding: janela completa de meses (cutoff → mês atual), mesmo os zerados.
    # União com os meses observados protege contra lançamentos fora do range.
    sorted_months = sorted(set(_full_month_keys(cutoff, today)) | all_months_set)
    month_labels: list[str] = []
    for mk in sorted_months:
        try:
            from datetime import date as _d
            month_labels.append(_d.fromisoformat(mk + "-01").strftime("%b/%y"))
        except ValueError:
            month_labels.append(mk)

    def _fmt(v: float) -> str:
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    grand = sum(
        sum(mv.values()) for mv in cat_monthly.values()
    )

    by_category = sorted(
        [
            {
                "label": lbl,
                "count": cat_counts[lbl],
                "total": sum(mv.values()),
                "total_str": _fmt(sum(mv.values())),
                "pct": round(sum(mv.values()) / grand * 100, 1) if grand > 0 else 0.0,
                "monthly": [mv.get(mk, 0.0) for mk in sorted_months],
            }
            for lbl, mv in cat_monthly.items()
        ],
        key=lambda x: -x["total"],
    )

    return {
        "transactions": transactions,
        "by_category": by_category,
        "month_labels": month_labels,
        "months": sorted_months,
        "total": grand,
        "total_str": _fmt(grand),
        "count": len(transactions),
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_expense_anomalies(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Detecta meses anômalos por fornecedor (> média + 1.5σ).

    Para cada fornecedor com >= 3 meses de histórico, calcula:
    - Média mensal de despesas pagas
    - Desvio padrão
    - Meses onde valor > média + 1.5 × σ

    Retorna lista de anomalias ordenada por excesso (valor - threshold).
    """
    import math
    from apps.financial.infrastructure.models import Expense as ExpenseModel

    today = timezone.now().date()
    cutoff = _first_of_month_n_ago(today, months)

    qs = (
        ExpenseModel.objects.filter(
            organization=organization,
            status="PAID",
            paid_at__isnull=False,
            paid_at__gte=cutoff,
        )
        .annotate(month=TruncMonth("paid_at"))
        .values("month", "supplier_name")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
        .order_by("supplier_name", "month")
    )

    # Group by supplier → {month → amount}
    supplier_data: dict[str, dict[str, float]] = {}
    for row in qs:
        mk = row["month"].strftime("%Y-%m")
        sn = row["supplier_name"] or "(sem fornecedor)"
        supplier_data.setdefault(sn, {})[mk] = float(row["total"])

    anomalies = []
    for supplier, monthly in supplier_data.items():
        if len(monthly) < 3:
            continue
        values = list(monthly.values())
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        std_dev = math.sqrt(variance)
        threshold = mean + 1.5 * std_dev
        for month_key, amount in monthly.items():
            if amount > threshold and amount > mean * 1.2:  # also require 20% above mean
                try:
                    from datetime import date as _d
                    label = _d.fromisoformat(month_key + "-01").strftime("%b/%y")
                except ValueError:
                    label = month_key
                anomalies.append({
                    "supplier": supplier,
                    "month": month_key,
                    "month_label": label,
                    "amount": amount,
                    "mean": mean,
                    "std_dev": std_dev,
                    "threshold": threshold,
                    "excess": amount - threshold,
                    "excess_pct": (amount / mean - 1) * 100 if mean > 0 else 0,
                })

    anomalies.sort(key=lambda a: -a["excess"])
    return anomalies


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_top_delinquent_invoices(
    organization: Organization, limit: int = 50
) -> list[dict[str, Any]]:
    """Top N invoices em atraso, ordenadas por dias de atraso."""
    rows = (
        FactInvoice.objects.filter(
            organization=organization,
            status__in=("PENDING", "OVERDUE"),
            days_overdue__gt=0,
        )
        .select_related("invoice__contract__customer")
        .order_by("-days_overdue")[:limit]
    )
    return [
        {
            "invoice_id": r.invoice.external_id,
            "customer_name": (
                r.invoice.contract.customer.name
                if r.invoice.contract and r.invoice.contract.customer
                else "—"
            ),
            "amount": float(r.amount),
            "due_date": r.due_date.isoformat(),
            "days_overdue": r.days_overdue,
            "bucket": r.aging_bucket,
        }
        for r in rows
    ]


# =============================================================================
# Sales / CRM — funil de vendas, origem de leads, net adds, pipeline
# =============================================================================
@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_sales_funnel(organization: Organization) -> dict[str, Any]:
    """Funil de vendas: leads → negociações → ganhos.

    Conversão lead→ganho = negociações WON ÷ total de leads. Pipeline aberto =
    soma do valor das negociações OPEN (receita potencial em andamento).
    """
    from apps.sales.infrastructure.models import Lead, Opportunity

    today = timezone.now().date()
    month_start = today.replace(day=1)

    total_leads = Lead.objects.filter(organization=organization).count()
    leads_new_month = Lead.objects.filter(
        organization=organization, created_at_source__date__gte=month_start
    ).count()

    opp_by_status = (
        Opportunity.objects.filter(organization=organization)
        .values("status")
        .annotate(
            count=Count("id"),
            total=Coalesce(Sum("value"), _ZERO, output_field=DecimalField()),
        )
    )
    counts: dict[str, int] = {}
    values: dict[str, Decimal] = {}
    for row in opp_by_status:
        counts[row["status"]] = row["count"]
        values[row["status"]] = row["total"]

    total_opps = sum(counts.values())
    won_count = counts.get("WON", 0)
    lost_count = counts.get("LOST", 0)
    open_count = counts.get("OPEN", 0)
    pipeline_value = values.get("OPEN", _ZERO)
    won_value = values.get("WON", _ZERO)

    # Taxas de conversão entre estágios do funil.
    lead_to_opp = round(total_opps / total_leads * 100, 1) if total_leads else 0.0
    opp_to_won = round(won_count / total_opps * 100, 1) if total_opps else 0.0
    lead_to_won = round(won_count / total_leads * 100, 1) if total_leads else 0.0

    funnel_stages = [
        {"stage": "Leads", "count": total_leads},
        {"stage": "Negociações", "count": total_opps},
        {"stage": "Ganhos", "count": won_count},
    ]

    return {
        "total_leads": total_leads,
        "leads_new_month": leads_new_month,
        "total_opportunities": total_opps,
        "won_count": won_count,
        "lost_count": lost_count,
        "open_count": open_count,
        "pipeline_value": float(pipeline_value),
        "won_value": float(won_value),
        "lead_to_opp_pct": lead_to_opp,
        "opp_to_won_pct": opp_to_won,
        "lead_to_won_pct": lead_to_won,
        "funnel_stages": funnel_stages,
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_lead_origin(organization: Organization) -> list[dict[str, Any]]:
    """Distribuição de leads por canal de origem (indicação, site, redes...).

    Leads sem origem informada são agrupados em "Não informado".
    """
    from apps.sales.infrastructure.models import Lead

    rows = (
        Lead.objects.filter(organization=organization)
        .values("origin")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [
        {"origin": (row["origin"] or "Não informado"), "count": row["count"]}
        for row in rows
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_net_adds_series(
    organization: Organization, months: int = 12
) -> list[dict[str, Any]]:
    """Série mensal de net adds = contratos ativados - cancelados no mês.

    Net adds positivo = base crescendo; negativo = base encolhendo. Usa
    `activated_at` (gross adds) e `canceled_at` (gross churn) do contrato.
    """
    from apps.customers.infrastructure.models import Contract

    today = timezone.now().date()
    series: list[dict[str, Any]] = []
    for i in range(months - 1, -1, -1):
        month_first = _first_of_month_n_ago(today, i)
        next_month_first = _first_of_month_n_ago(today, i - 1) if i > 0 else None

        adds_qs = Contract.objects.filter(
            organization=organization, activated_at__date__gte=month_first
        )
        churn_qs = Contract.objects.filter(
            organization=organization, canceled_at__date__gte=month_first
        )
        if next_month_first is not None:
            adds_qs = adds_qs.filter(activated_at__date__lt=next_month_first)
            churn_qs = churn_qs.filter(canceled_at__date__lt=next_month_first)

        adds = adds_qs.count()
        churn = churn_qs.count()
        series.append({
            "month": month_first.strftime("%Y-%m"),
            "label": month_first.strftime("%b/%y"),
            "adds": adds,
            "churn": churn,
            "net": adds - churn,
        })
    return series


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_pipeline_aging(
    organization: Organization, limit: int = 50
) -> list[dict[str, Any]]:
    """Negociações abertas (OPEN) ordenadas por tempo em aberto (aging).

    Negociações paradas há muito tempo indicam gargalo no funil — base pra
    ação comercial.
    """
    from apps.sales.infrastructure.models import Opportunity

    today = timezone.now().date()
    rows = (
        Opportunity.objects.filter(organization=organization, status="OPEN")
        .select_related("lead")
        .order_by("created_at_source")[:limit]
    )
    result: list[dict[str, Any]] = []
    for opp in rows:
        created = opp.created_at_source.date() if opp.created_at_source else None
        days_open = (today - created).days if created else 0
        result.append({
            "external_id": opp.external_id,
            "lead_name": (opp.lead.name if opp.lead else "")
            or f"Lead #{opp.lead_external_id}",
            "value": float(opp.value),
            "created_at": created.isoformat() if created else "—",
            "days_open": days_open,
        })
    return result


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_bandwidth_summary(
    organization: Organization, top: int = 20
) -> dict[str, Any]:
    """Resumo do consumo de banda agregado por cliente.

    Soma download/upload de todos os registros de consumo e identifica os
    maiores consumidores — base pra segmentação (heavy users → upgrade,
    subutilização → downgrade/churn) e dimensionamento de backbone.
    """
    from apps.network.infrastructure.models import BandwidthUsage

    qs = BandwidthUsage.objects.filter(organization=organization)

    totals = qs.aggregate(
        total_download=Coalesce(Sum("download_bytes"), 0),
        total_upload=Coalesce(Sum("upload_bytes"), 0),
    )
    total_download = totals["total_download"] or 0
    total_upload = totals["total_upload"] or 0
    total_bytes = total_download + total_upload

    # Clientes distintos com consumo registrado
    customer_count = (
        qs.exclude(customer_external_id="")
        .values("customer_external_id")
        .distinct()
        .count()
    )

    avg_per_customer = (total_bytes / customer_count) if customer_count else 0

    # Top consumidores agregados por cliente
    rows = (
        qs.values("customer_external_id", "customer__name")
        .annotate(
            download=Coalesce(Sum("download_bytes"), 0),
            upload=Coalesce(Sum("upload_bytes"), 0),
        )
        .order_by("-download")
    )
    top_consumers: list[dict[str, Any]] = []
    for row in rows[:top]:
        dl = row["download"] or 0
        ul = row["upload"] or 0
        total = dl + ul
        ext = row["customer_external_id"]
        top_consumers.append({
            "customer_external_id": ext,
            "customer_name": row["customer__name"] or f"Cliente #{ext}",
            "download_bytes": dl,
            "upload_bytes": ul,
            "total_bytes": total,
            "total_gb": round(total / 1024**3, 2),
        })

    return {
        "total_download_bytes": total_download,
        "total_upload_bytes": total_upload,
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1024**3, 2),
        "customer_count": customer_count,
        "avg_per_customer_gb": round(avg_per_customer / 1024**3, 2),
        "top_consumers": top_consumers,
    }


# =============================================================================
# Customer 360 — visão unificada do cliente (read-only, cross-app)
# =============================================================================
_OPEN_TICKET_STATUSES = ("OPEN", "SCHEDULED", "IN_PROGRESS", "FORWARDED")


@allow_cross_tenant(reason="busca de clientes roda no escopo da org passada explicitamente")
def search_customers(
    organization: Organization, query: str = "", limit: int = 50
) -> list[dict[str, Any]]:
    """Lista clientes da org, filtrando por nome, documento ou external_id.

    Busca substring case-insensitive em name/document/external_id. Sem query,
    retorna os primeiros `limit` clientes por nome — base pra navegação inicial.
    """
    from apps.customers.infrastructure.models import Customer

    qs = Customer.objects.filter(organization=organization)
    q = (query or "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(document__icontains=q)
            | Q(external_id__icontains=q)
        )

    rows = (
        qs.annotate(
            contract_count=Count("contracts", distinct=True),
            active_contracts=Count(
                "contracts", filter=Q(contracts__status="ACTIVE"), distinct=True
            ),
        )
        .order_by("name")[:limit]
    )

    return [
        {
            "id": c.id,
            "external_id": c.external_id,
            "name": c.name,
            "document": c.document,
            "status": c.status,
            "contract_count": c.contract_count,
            "active_contracts": c.active_contracts,
        }
        for c in rows
    ]


# Recomendação de ação por sinal de risco — orienta o atendimento no 360.
_CHURN_RECOMMENDATIONS: dict[str, str] = {
    "CONTRACT_BLOCKED": (
        "Negociar desbloqueio: contato para acordo de pagamento e religação."
    ),
    "LATE_PAYMENTS": (
        "Acionar cobrança ativa: oferecer renegociação ou parcelamento do débito."
    ),
    "FREQUENT_TICKETS": (
        "Revisar qualidade técnica: agendar diagnóstico de rede para resolver a "
        "recorrência de chamados."
    ),
    "OFFLINE": (
        "Verificar conexão: cliente offline com contrato ativo — checar "
        "equipamento e rede."
    ),
    "PLAN_DOWNGRADE": (
        "Entender o downgrade: sondar insatisfação e oferecer plano de retenção."
    ),
    "BANDWIDTH_DROP": (
        "Investigar queda de uso: possível migração para concorrente — fazer "
        "contato proativo."
    ),
    "ML_HIGH_RISK": (
        "Risco previsto pelo modelo: priorizar contato de relacionamento e retenção."
    ),
}


def _whatsapp_link(phone: str | None) -> str | None:
    """Monta um link wa.me a partir do telefone do cliente (formato BR).

    Mantém só os dígitos e garante o DDI 55: números com 10–11 dígitos (DDD +
    assinante) recebem o 55; números já com 12–13 dígitos são usados como estão.
    Fora dessas faixas, retorna None (não dá pra montar um link confiável).
    """
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) in (10, 11):
        digits = "55" + digits
    elif len(digits) not in (12, 13):
        return None
    return f"https://wa.me/{digits}"


@allow_cross_tenant(reason="agregação cross-app roda no escopo da org/cliente passados")
def compute_customer_360(
    organization: Organization, customer: Any
) -> dict[str, Any]:
    """Visão unificada read-only de um cliente: contratos, financeiro, suporte, rede.

    Junta dados dos bounded contexts (customers, financial, helpdesk, network,
    inventory) numa única estrutura. Puramente analítico — nenhuma ação/edição.
    Base pra atendimento rápido, identificação de risco de churn e CLV.
    """
    from apps.customers.infrastructure.models import Contract
    from apps.financial.infrastructure.models import Invoice, Payment
    from apps.helpdesk.infrastructure.models import Ticket
    from apps.inventory.infrastructure.models import ContractEquipment
    from apps.network.infrastructure.models import BandwidthUsage, Connection

    today = timezone.now().date()
    timeline: list[dict[str, Any]] = []

    # ── Contratos ──────────────────────────────────────────────────────
    contracts = list(
        Contract.objects.filter(
            organization=organization, customer=customer
        ).order_by("-activated_at")
    )
    contract_rows: list[dict[str, Any]] = []
    mrr_active = _ZERO
    for c in contracts:
        net = c.monthly_amount_net
        if c.status == "ACTIVE":
            mrr_active += net
        contract_rows.append({
            "external_id": c.external_id,
            "plan_name": c.plan_name,
            "status": c.status,
            "monthly_amount": float(c.monthly_amount or 0),
            "monthly_amount_addons": float(c.monthly_amount_addons or 0),
            "monthly_amount_discounts": float(c.monthly_amount_discounts or 0),
            "monthly_amount_net": float(net),
            "activated_at": c.activated_at,
            "canceled_at": c.canceled_at,
            "address": c.address,
        })
        if c.activated_at:
            timeline.append({
                "at": c.activated_at,
                "type": "contract_activated",
                "label": f"Contrato ativado · {c.plan_name}",
            })
        if c.canceled_at:
            timeline.append({
                "at": c.canceled_at,
                "type": "contract_canceled",
                "label": f"Contrato cancelado · {c.plan_name}",
            })

    # ── Financeiro ─────────────────────────────────────────────────────
    invoices_qs = Invoice.objects.filter(
        organization=organization, contract__customer=customer
    )
    pending_q = Q(status="PENDING") | Q(status="OVERDUE")
    overdue_amount = invoices_qs.filter(
        pending_q, due_date__lt=today
    ).aggregate(
        total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField())
    )["total"]
    open_amount = invoices_qs.filter(pending_q).aggregate(
        total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField())
    )["total"]
    paid_total = invoices_qs.filter(status="PAID").aggregate(
        total=Coalesce(Sum("paid_amount"), _ZERO, output_field=DecimalField())
    )["total"]

    recent_invoices = []
    for inv in invoices_qs.order_by("-due_date")[:12]:
        is_overdue = inv.status in ("PENDING", "OVERDUE") and inv.due_date < today
        days_overdue = (today - inv.due_date).days if is_overdue else 0
        recent_invoices.append({
            "external_id": inv.external_id,
            "amount": float(inv.amount or 0),
            "due_date": inv.due_date,
            "status": inv.status,
            "paid_at": inv.paid_at,
            "is_overdue": is_overdue,
            "days_overdue": days_overdue,
        })

    payments_qs = Payment.objects.filter(
        organization=organization, contract__customer=customer
    )
    recent_payments = [
        {
            "external_id": p.external_id,
            "amount": float(p.amount or 0),
            "paid_at": p.paid_at,
            "method": p.method,
        }
        for p in payments_qs.order_by("-paid_at")[:12]
    ]
    for p in payments_qs.order_by("-paid_at")[:20]:
        timeline.append({
            "at": p.paid_at,
            "type": "payment",
            "label": f"Pagamento · R$ {p.amount:.2f} ({p.get_method_display()})",
        })

    delinquent = overdue_amount > _ZERO

    # ── Suporte ────────────────────────────────────────────────────────
    tickets_qs = Ticket.objects.filter(
        organization=organization, customer=customer
    )
    open_tickets_qs = tickets_qs.filter(status__in=_OPEN_TICKET_STATUSES)
    open_tickets_count = open_tickets_qs.count()
    tickets_total = tickets_qs.count()

    recent_tickets = [
        {
            "external_id": t.external_id,
            "protocol": t.protocol,
            "status": t.status,
            "priority": t.priority,
            "sector": t.sector,
            "subject": (t.message or "")[:120],
            "opened_at": t.opened_at,
            "closed_at": t.closed_at,
        }
        for t in tickets_qs.order_by("-opened_at")[:10]
    ]
    for t in tickets_qs.order_by("-opened_at")[:20]:
        if t.opened_at:
            timeline.append({
                "at": t.opened_at,
                "type": "ticket",
                "label": f"Chamado aberto · {t.protocol or t.external_id}",
            })

    # SLA: tempo médio de resolução (horas) dos chamados fechados
    closed = tickets_qs.filter(
        status="CLOSED", opened_at__isnull=False, closed_at__isnull=False
    )
    durations = [
        (t.closed_at - t.opened_at).total_seconds() / 3600.0
        for t in closed
    ]
    avg_resolution_hours = (
        round(sum(durations) / len(durations), 1) if durations else None
    )

    # ── Rede ───────────────────────────────────────────────────────────
    connections = [
        {
            "login": cn.login,
            "status": cn.status,
            "ip": cn.ip,
            "nas_ip": cn.nas_ip,
            "last_connection_at": cn.last_connection_at,
        }
        for cn in Connection.objects.filter(
            organization=organization, customer=customer
        ).order_by("login")
    ]

    bw_qs = BandwidthUsage.objects.filter(
        organization=organization, customer=customer
    )
    bw_totals = bw_qs.aggregate(
        download=Coalesce(Sum("download_bytes"), 0),
        upload=Coalesce(Sum("upload_bytes"), 0),
    )
    bw_download = bw_totals["download"] or 0
    bw_upload = bw_totals["upload"] or 0
    bw_total = bw_download + bw_upload

    # ── Equipamentos em comodato ───────────────────────────────────────
    equipment = [
        {
            "product_name": e.product_name,
            "serial": e.serial,
            "mac": e.mac,
            "status": e.status,
            "value": float(e.value or 0),
        }
        for e in ContractEquipment.objects.filter(
            organization=organization, contract__customer=customer
        ).order_by("-status", "product_name")
    ]

    # ── Cadastro (timeline) ────────────────────────────────────────────
    if customer.created_at_source:
        timeline.append({
            "at": customer.created_at_source,
            "type": "customer_created",
            "label": "Cliente cadastrado",
        })

    timeline.sort(key=lambda ev: ev["at"], reverse=True)
    timeline = timeline[:30]

    # ── Risco de churn (lê o score materializado) ──────────────────────
    risk_row = ChurnRiskScore.objects.filter(
        organization=organization, customer=customer
    ).first()
    churn: dict[str, Any] | None = None
    if risk_row is not None:
        signals = risk_row.signals or []
        recommendations = [
            {
                "code": s.get("code", ""),
                "label": s.get("label", ""),
                "text": _CHURN_RECOMMENDATIONS[s["code"]],
            }
            for s in signals
            if s.get("code") in _CHURN_RECOMMENDATIONS
        ]
        ml_pct = (
            int(round(float(risk_row.ml_probability) * 100))
            if risk_row.ml_probability is not None
            else None
        )
        churn = {
            "level": risk_row.level,
            "score": risk_row.score,
            "signals": signals,
            "recommendations": recommendations,
            "ml_probability_pct": ml_pct,
            "computed_at": risk_row.computed_at,
        }

    return {
        "customer": {
            "id": customer.id,
            "external_id": customer.external_id,
            "name": customer.name,
            "document": customer.document,
            "email": customer.email,
            "phone": customer.phone,
            "whatsapp_url": _whatsapp_link(customer.phone),
            "status": customer.status,
            "created_at_source": customer.created_at_source,
            "source_type": customer.source_type,
        },
        "churn": churn,
        "contracts": contract_rows,
        "contracts_count": len(contract_rows),
        "mrr_active": float(mrr_active),
        "financial": {
            "overdue_amount": float(overdue_amount),
            "open_amount": float(open_amount),
            "paid_total": float(paid_total),
            "delinquent": delinquent,
            "recent_invoices": recent_invoices,
            "recent_payments": recent_payments,
        },
        "support": {
            "open_count": open_tickets_count,
            "total_count": tickets_total,
            "avg_resolution_hours": avg_resolution_hours,
            "recent_tickets": recent_tickets,
        },
        "network": {
            "connections": connections,
            "download_bytes": bw_download,
            "upload_bytes": bw_upload,
            "total_bytes": bw_total,
            "total_gb": round(bw_total / 1024**3, 2),
        },
        "equipment": equipment,
        "timeline": timeline,
    }


# =============================================================================
# Churn risk — resumo + top clientes em risco (lê ChurnRiskScore)
# =============================================================================
@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_churn_risk_summary(organization: Organization) -> dict[str, Any]:
    """KPIs de risco de churn — contagem por nível, receita em risco e sinais.

    Lê os scores já materializados por `compute_churn_risk_scores` (Celery
    diário). `revenue_at_risk` soma a mensalidade líquida dos clientes
    HIGH+MEDIUM — os acionáveis pra retenção.
    """
    qs = ChurnRiskScore.objects.filter(organization=organization)

    by_level = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for row in qs.values("level").annotate(n=Count("id")):
        by_level[row["level"]] = row["n"]

    revenue_at_risk = float(
        qs.filter(level__in=("HIGH", "MEDIUM")).aggregate(
            s=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField())
        )["s"] or 0
    )
    revenue_total = float(
        qs.aggregate(
            s=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField())
        )["s"] or 0
    )

    # Distribuição de sinais — itera os JSON (lista pequena por cliente)
    signal_counts: dict[str, dict[str, Any]] = {}
    for sig_list in qs.values_list("signals", flat=True):
        for s in sig_list or []:
            code = s.get("code", "?")
            entry = signal_counts.setdefault(
                code, {"code": code, "label": s.get("label", code), "count": 0}
            )
            entry["count"] += 1
    signal_distribution = sorted(
        signal_counts.values(), key=lambda e: e["count"], reverse=True
    )

    last_computed = qs.aggregate(m=Max("computed_at"))["m"]

    return {
        "total_at_risk": by_level["HIGH"] + by_level["MEDIUM"] + by_level["LOW"],
        "high": by_level["HIGH"],
        "medium": by_level["MEDIUM"],
        "low": by_level["LOW"],
        "revenue_at_risk": revenue_at_risk,
        "revenue_total": revenue_total,
        "signal_distribution": signal_distribution,
        "computed_at": last_computed,
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_top_risk_customers(
    organization: Organization, limit: int = 20
) -> list[dict[str, Any]]:
    """Top clientes em risco de churn, ordenados por score desc.

    Junta o score materializado com o cliente (nome/documento/status) pra
    alimentar a tabela do dashboard de alertas, com link pro Customer 360.
    """
    rows = (
        ChurnRiskScore.objects.filter(organization=organization)
        .select_related("customer")
        .order_by("-score", "-monthly_amount")[:limit]
    )
    return [
        {
            "customer_id": r.customer_id,
            "name": r.customer.name,
            "document": r.customer.document,
            "status": r.customer.status,
            "score": r.score,
            "level": r.level,
            "monthly_amount": float(r.monthly_amount or 0),
            "ml_probability": float(r.ml_probability) if r.ml_probability is not None else None,
            "ml_probability_pct": (
                int(round(float(r.ml_probability) * 100))
                if r.ml_probability is not None
                else None
            ),
            "signals": r.signals or [],
        }
        for r in rows
    ]


# Sinais de natureza financeira → ação de cobrança; o resto → retenção.
_PAYMENT_SIGNAL_CODES = {"LATE_PAYMENTS", "CONTRACT_BLOCKED"}


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_priority_customers(
    organization: Organization, limit: int = 15
) -> dict[str, Any]:
    """Clientes a focar — prioriza por valor × risco e sugere a ação (#28).

    A página de clientes era só busca plana. Aqui combinamos o score de churn já
    materializado (`ChurnRiskScore`) com o valor do cliente (MRR líquido) num
    **índice de foco** = (valor norm.) × (risco norm.) — quem é caro E arriscado
    sobe ao topo. O pool de foco são os níveis HIGH/MEDIUM (os acionáveis p/
    retenção, mesma base de `revenue_at_risk`).

    A ação sai dos sinais: sinal financeiro (atraso/bloqueio) → **COBRAR**; senão
    → **RETER**. Em paralelo, listamos candidatos a **UPSELL**: clientes ACTIVE
    de maior MRR que NÃO estão no radar de risco (saudáveis, com espaço p/ crescer).
    Tudo JSON-friendly pra alimentar o template.
    """
    scores = list(
        ChurnRiskScore.objects.filter(
            organization=organization, level__in=("HIGH", "MEDIUM")
        )
        .select_related("customer")
        .order_by("-score", "-monthly_amount")
    )

    max_value = max((float(s.monthly_amount or 0) for s in scores), default=0.0) or 1.0
    max_score = max((s.score for s in scores), default=0) or 1

    focus: list[dict[str, Any]] = []
    cobrar_count = 0
    reter_count = 0
    revenue_in_focus = 0.0
    for s in scores:
        value = float(s.monthly_amount or 0)
        value_norm = value / max_value
        risk_norm = s.score / max_score
        focus_index = round(value_norm * risk_norm * 100, 1)

        sigs = sorted(
            s.signals or [], key=lambda x: x.get("weight", 0), reverse=True
        )
        codes = {sig.get("code") for sig in sigs}
        action = "COBRAR" if codes & _PAYMENT_SIGNAL_CODES else "RETER"
        if action == "COBRAR":
            cobrar_count += 1
        else:
            reter_count += 1
        revenue_in_focus += value

        reason_labels = [sig.get("label", "") for sig in sigs[:2] if sig.get("label")]
        focus.append(
            {
                "customer_id": s.customer_id,
                "name": s.customer.name,
                "document": s.customer.document,
                "status": s.customer.status,
                "level": s.level,
                "score": s.score,
                "monthly_amount": value,
                "focus_index": focus_index,
                "action": action,
                "reason": " · ".join(reason_labels) or "Risco elevado",
                "ml_probability_pct": (
                    int(round(float(s.ml_probability) * 100))
                    if s.ml_probability is not None
                    else None
                ),
            }
        )

    focus.sort(key=lambda r: r["focus_index"], reverse=True)

    # UPSELL: clientes ACTIVE de maior MRR fora do radar de risco.
    risk_ids = set(
        ChurnRiskScore.objects.filter(organization=organization).values_list(
            "customer_id", flat=True
        )
    )
    today = timezone.now().date()
    upsell_rows = (
        FactContractStatusDaily.objects.filter(
            organization=organization, date=today, is_active=True, status="ACTIVE"
        )
        .values("contract__customer_id", "contract__customer__name")
        .annotate(mrr=Coalesce(Sum("monthly_amount"), _ZERO, output_field=DecimalField()))
        .order_by("-mrr")
    )
    upsell: list[dict[str, Any]] = []
    for row in upsell_rows:
        cid = row["contract__customer_id"]
        if cid is None or cid in risk_ids:
            continue
        upsell.append(
            {
                "customer_id": cid,
                "name": row["contract__customer__name"] or "—",
                "monthly_amount": float(row["mrr"] or 0),
                "action": "UPSELL",
            }
        )
        if len(upsell) >= limit:
            break

    return {
        "focus": focus[:limit],
        "upsell": upsell,
        "focus_count": len(focus),
        "cobrar_count": cobrar_count,
        "reter_count": reter_count,
        "upsell_count": len(upsell),
        "revenue_in_focus": revenue_in_focus,
    }
