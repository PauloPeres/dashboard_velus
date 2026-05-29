"""Agregações analíticas — MRR, churn, ARPU, inadimplência, cash forecast.

Operam sobre fact tables, retornam dicts simples (JSON-friendly) prontos pra
view/template/Plotly. NUNCA tocam domain models diretamente.

Cache: cada função aceita `use_cache=True` e usa Redis com TTL+invalidação
por signal `sync_completed` (futuro).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, DecimalField, Sum
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
        # Pega último dia do mês i atrás
        target_month_first = (today.replace(day=1) - timedelta(days=i * 30)).replace(day=1)
        if i > 0:
            next_first = (target_month_first + timedelta(days=32)).replace(day=1)
            sample_date = next_first - timedelta(days=1)
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

    # Contratos ativos
    active_count = FactContractStatusDaily.objects.filter(
        organization=organization, date=today, is_active=True
    ).count()

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
