"""Agregações analíticas — MRR, churn, ARPU, inadimplência, cash forecast.

Operam sobre fact tables, retornam dicts simples (JSON-friendly) prontos pra
view/template/Plotly. NUNCA tocam domain models diretamente.

Cache: cada função aceita `use_cache=True` e usa Redis com TTL+invalidação
por signal `sync_completed` (futuro).
"""

from __future__ import annotations

from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, DecimalField, Max, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone

from apps.analytics.infrastructure.models import (
    DimContract,
    FactContractStatusDaily,
    FactExpense,
    FactInvoice,
    FactPayment,
)
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_ZERO = Decimal("0.00")


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
    """Recebimentos por mês — entrada de caixa real."""
    today = timezone.now().date()
    cutoff = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    by_month = (
        FactPayment.objects.filter(organization=organization, paid_date__gte=cutoff)
        .annotate(month=TruncMonth("paid_date"))
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
    """Top fornecedores por despesa paga nos últimos N meses."""
    today = timezone.now().date()
    cutoff = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    by_supplier = (
        FactExpense.objects.filter(
            organization=organization,
            status="PAID",
            expense_date__gte=cutoff,
        )
        .values("supplier_name")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("-total")[:20]
    )
    return [
        {
            "supplier": row["supplier_name"] or "Sem fornecedor",
            "amount": float(row["total"]),
            "count": row["count"],
        }
        for row in by_supplier
    ]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_expense_by_category(
    organization: Organization, months: int = 3
) -> list[dict[str, Any]]:
    """Distribuição de despesas pagas por categoria nos últimos N meses."""
    today = timezone.now().date()
    cutoff = (today.replace(day=1) - timedelta(days=months * 31)).replace(day=1)
    by_cat = (
        FactExpense.objects.filter(
            organization=organization,
            status="PAID",
            expense_date__gte=cutoff,
        )
        .values("category")
        .annotate(
            total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()),
            count=Count("id"),
        )
        .order_by("-total")
    )
    return [
        {
            "category": row["category"] or "Sem categoria",
            "amount": float(row["total"]),
            "count": row["count"],
        }
        for row in by_cat
    ]


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

    # Current month (last entry)
    current_mrr = mrr_series[-1]["mrr"] if mrr_series else 0.0
    exp_by_month = {e["month"]: e["expenses"] for e in expense_series}
    current_month_key = mrr_series[-1]["month"] if mrr_series else ""
    current_expenses = exp_by_month.get(current_month_key, 0.0)
    current_ebitda = current_mrr - current_expenses
    current_margin = (
        float(current_ebitda / current_mrr * 100) if current_mrr > 0 else 0.0
    )

    # YTD: sum all months
    ytd_revenue = sum(m["mrr"] for m in mrr_series)
    ytd_expenses = sum(e["expenses"] for e in expense_series)
    ytd_ebitda = ytd_revenue - ytd_expenses

    return {
        "mrr_series": mrr_series,
        "expense_series": expense_series,
        "cashflow_series": cashflow_series,
        "current_month": {
            "receita_bruta": current_mrr,
            "despesas": current_expenses,
            "ebitda": current_ebitda,
            "ebitda_margin_pct": current_margin,
        },
        "ytd": {
            "receita_bruta": ytd_revenue,
            "despesas": ytd_expenses,
            "ebitda": ytd_ebitda,
        },
    }


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_revenue_forecast(
    organization: Organization, months_ahead: int = 12
) -> list[dict[str, Any]]:
    """Previsão 12m baseada em tendência de MRR.

    Usa média de crescimento dos últimos 3 meses de MRR para projetar forward.
    Também projeta despesas com base na média dos últimos 3 meses.
    """
    from datetime import date as date_cls

    # Historical base: last 6 months
    hist_mrr = compute_mrr_series(organization, months=6)
    hist_exp = compute_expense_series(organization, months=6)

    # Compute avg MRR growth from last 3 months
    mrr_values = [m["mrr"] for m in hist_mrr]
    if len(mrr_values) >= 3:
        last3 = mrr_values[-3:]
        growth = (last3[-1] / last3[0]) ** (1 / 2) - 1 if last3[0] > 0 else 0.0  # compound monthly
    elif len(mrr_values) >= 2:
        growth = (mrr_values[-1] / mrr_values[0] - 1) if mrr_values[0] > 0 else 0.0
    else:
        growth = 0.0

    # Cap growth between -20% and +20% per month
    growth = max(-0.20, min(0.20, growth))

    # Avg monthly expenses (last 3 paid months)
    exp_values = [e["expenses"] for e in hist_exp if e["expenses"] > 0]
    avg_exp = sum(exp_values[-3:]) / len(exp_values[-3:]) if exp_values else 0.0

    # Base MRR = last historical value
    base_mrr = mrr_values[-1] if mrr_values else 0.0

    today = timezone.now().date()
    # Start forecast from next month
    next_month_first = (today.replace(day=1))
    # Advance to first forecast month
    nm = next_month_first.month + 1
    ny = next_month_first.year
    if nm > 12:
        nm = 1
        ny += 1
    forecast_start = date_cls(ny, nm, 1)

    result = []
    for i in range(months_ahead):
        m = forecast_start.month + i
        y = forecast_start.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        d = date_cls(y, m, 1)
        month_key = d.strftime("%Y-%m")
        label = d.strftime("%b/%y")
        forecast_mrr = base_mrr * ((1 + growth) ** (i + 1))
        forecast_net = forecast_mrr - avg_exp
        result.append(
            {
                "month": month_key,
                "label": label,
                "forecast_mrr": round(forecast_mrr, 2),
                "forecast_expenses": round(avg_exp, 2),
                "forecast_net": round(forecast_net, 2),
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

    at_risk_ids = []
    for cid in blocked_ids:
        last_ok = last_non_blocked.get(cid)
        days = (today - last_ok).days - 1 if last_ok else 999
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
        last_ok = last_non_blocked.get(cid)
        days = (today - last_ok).days - 1 if last_ok else 999
        if days < min_days:
            continue
        c = contracts_map.get(cid)
        if not c:
            continue
        blocked_since = (last_ok + timedelta(days=1)).isoformat() if last_ok else "—"
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
        status="CANCELED",
        canceled_at__date__gte=cutoff,
    )

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
        status="CANCELED",
        canceled_at__isnull=False,
        activated_at__isnull=False,
    ).iterator()

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
        status="CANCELED",
        canceled_at__date__gte=cutoff,
        activated_at__isnull=False,
    )

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

    # Apenas contratos que foram ativados — pré-contratos abandonados não geram MRR
    canceled_qs = Contract.objects.filter(
        organization=organization, status="CANCELED", activated_at__isnull=False
    )

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
