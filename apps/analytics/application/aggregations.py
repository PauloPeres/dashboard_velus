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

from django.db.models import Count, DecimalField, Max, OuterRef, Q, Subquery, Sum
from django.db.models.fields.json import KeyTextTransform
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
    # Agrupa por id_planejamento (conta pai) — múltiplos id_conta podem ter o mesmo pai
    parent_map: dict[str, dict[str, Any]] = {}
    for row in qs:
        id_conta = str(row["id_conta_str"] or "0")
        id_plan = _CONTA_TO_PLANO.get(id_conta, "0")
        entry = _PLANEJAMENTO.get(id_plan, {})
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

    # Current month (last entry)
    current_mrr = mrr_series[-1]["mrr"] if mrr_series else 0.0
    exp_by_month = {e["month"]: e["expenses"] for e in expense_series}
    current_month_key = mrr_series[-1]["month"] if mrr_series else ""
    current_expenses = exp_by_month.get(current_month_key, 0.0)
    current_ebitda = current_mrr - current_expenses
    current_margin = (
        float(current_ebitda / current_mrr * 100) if current_mrr > 0 else 0.0
    )

    # YTD: sum only months in the current year
    current_year = str(timezone.now().year)
    ytd_revenue = sum(m["mrr"] for m in mrr_series if m["month"].startswith(current_year))
    ytd_expenses = sum(e["expenses"] for e in expense_series if e["month"].startswith(current_year))
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
    """Previsão 12m baseada em tendência de MRR ajustada pela taxa de recebimento.

    Usa média de crescimento dos últimos 3 meses de MRR para projetar forward.
    Aplica taxa de recebimento histórica (caixa recebido / MRR) para refletir
    o impacto real da inadimplência na receita efetiva.
    Também projeta despesas com base na média dos últimos 3 meses.
    """
    from datetime import date as date_cls

    # Historical base: last 6 months
    hist_mrr = compute_mrr_series(organization, months=6)
    hist_exp = compute_expense_series(organization, months=6)
    hist_cash = compute_cash_received_series(organization, months=6)

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

    # Compute collection rate: avg(cash_received / mrr) for months with both values
    mrr_by_month = {m["month"]: m["mrr"] for m in hist_mrr if m["mrr"] > 0}
    cash_by_month = {c["month"]: c["amount"] for c in hist_cash if c["amount"] > 0}
    common_months = sorted(set(mrr_by_month) & set(cash_by_month))
    rates = [cash_by_month[m] / mrr_by_month[m] for m in common_months if mrr_by_month[m] > 0]
    collection_rate = sum(rates[-3:]) / len(rates[-3:]) if rates else 1.0
    # Cap: min 50%, max 105% (small overpayments happen)
    collection_rate = max(0.50, min(1.05, collection_rate))

    # Avg monthly expenses (last 3 paid months)
    exp_values = [e["expenses"] for e in hist_exp if e["expenses"] > 0]
    avg_exp = sum(exp_values[-3:]) / len(exp_values[-3:]) if exp_values else 0.0

    # Base MRR = last historical value
    base_mrr = mrr_values[-1] if mrr_values else 0.0

    today = timezone.now().date()
    # Start forecast from next month
    nm = today.replace(day=1).month + 1
    ny = today.replace(day=1).year
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
        # Receita efetiva = MRR × taxa de recebimento (ajuste de inadimplência)
        forecast_cash = forecast_mrr * collection_rate
        forecast_net = forecast_cash - avg_exp
        result.append(
            {
                "month": month_key,
                "label": label,
                "forecast_mrr": round(forecast_mrr, 2),
                "forecast_cash": round(forecast_cash, 2),   # receita ajustada por inadimplência
                "forecast_expenses": round(avg_exp, 2),
                "forecast_net": round(forecast_net, 2),
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


def _resolve_conta(id_conta: str | None) -> dict[str, str]:
    """Dado o id_conta (planejamento_analitico.id), retorna o planejamento pai.

    Fluxo: id_conta → _CONTA_TO_PLANO → id_planejamento → _PLANEJAMENTO → {cod, nome, tipo}
    """
    ic = str(id_conta or "0").strip()
    id_plan = _CONTA_TO_PLANO.get(ic, "0")
    return _PLANEJAMENTO.get(id_plan) or _PLANEJAMENTO.get("0") or {"cod": "", "nome": f"Conta #{ic}", "tipo": "?"}


def _get_planeja_label(id_conta: str | None) -> str:
    """Retorna label do planejamento pai: 'cod — nome' ou fallback.

    Usa id_conta (planejamento_analitico.id) para resolver via _CONTA_TO_PLANO.
    """
    entry = _resolve_conta(id_conta)
    return f"{entry['cod']} {entry['nome']}".strip() or "(Sem categoria)"


def _get_dre_section(cod: str) -> tuple[str, int]:
    """Mapeia cod contábil para (seção DRE, ordem).

    Verifica prefixo de dois segmentos antes de um — ex: "5.2" tem prioridade
    sobre "5". Usado para montar a estrutura de linhas da DRE.
    """
    if not cod:
        return ("Sem Categoria", 99)
    parts = cod.strip().rstrip(".").split(".")
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
    # Depois agrupa por id_planejamento (conta pai) via _CONTA_TO_PLANO
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
        id_plan = _CONTA_TO_PLANO.get(ic, "0")
        for mk, val in monthly_data.items():
            raw.setdefault(id_plan, {})[mk] = raw.get(id_plan, {}).get(mk, 0.0) + val

    # --- Query 2: breakdown por fornecedor dentro de cada conta ---
    qs_sup = (
        ExpenseModel.objects.filter(**_base_filter)
        .annotate(
            month=TruncMonth("paid_at"),
            id_conta_str=KeyTextTransform("id_conta", "raw_extras"),
        )
        .values("month", "id_conta_str", "supplier_name")
        .annotate(total=Coalesce(Sum("amount"), _ZERO, output_field=DecimalField()))
        .order_by("id_conta_str", "supplier_name", "month")
    )
    # {id_planejamento → {supplier → {YYYY-MM → float}}} — agrega por conta pai
    supplier_raw: dict[str, dict[str, dict[str, float]]] = {}
    for row in qs_sup:
        ic = str(row["id_conta_str"] or "0")
        id_plan = _CONTA_TO_PLANO.get(ic, "0")
        sup = (row["supplier_name"] or "").strip() or "(Sem fornecedor)"
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
        entry = _PLANEJAMENTO.get(id_plan, {})
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

    sorted_months = sorted(all_months_set)
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
