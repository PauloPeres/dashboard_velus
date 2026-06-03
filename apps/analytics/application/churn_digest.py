"""Digest de risco de churn por email — semanal e mensal (opt-in por usuário).

Compila o panorama de risco da org (a partir de `ChurnRiskScore`, já
computado pelo engine de regras + ML) e envia por email aos usuários que
optaram por receber (`User.churn_digest_weekly` / `churn_digest_monthly`).

Puramente informativo — reporting, não dispara nenhuma ação operacional.
"""

from __future__ import annotations

from typing import Any

import structlog
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from apps.analytics.application.aggregations import (
    compute_aging_distribution,
    compute_cash_received_series,
    compute_churn_risk_summary,
    compute_kpis,
    compute_revenue_forecast,
    compute_top_risk_customers,
)
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, User

_logger = structlog.get_logger(__name__)

PERIOD_WEEKLY = "weekly"
PERIOD_MONTHLY = "monthly"

# Mensal é o "completo"; semanal é o resumo enxuto.
_TOP_LIMIT = {PERIOD_WEEKLY: 10, PERIOD_MONTHLY: 50}
_PERIOD_LABEL = {PERIOD_WEEKLY: "semanal", PERIOD_MONTHLY: "mensal"}
_OPTIN_FIELD = {
    PERIOD_WEEKLY: "churn_digest_weekly",
    PERIOD_MONTHLY: "churn_digest_monthly",
}


def _fmt_brl(value: Any) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _build_strategic_block(organization: Organization) -> dict[str, Any]:
    """Bloco estratégico do digest mensal: MRR, churn, net adds, caixa, inadimplência, forecast."""
    kpis = compute_kpis(organization)
    current_key = timezone.now().strftime("%Y-%m")
    cash_series = compute_cash_received_series(organization, months=12)
    # paid_date do IXC tem registros futuros; busca o mês corrente pela chave.
    cash_month = next(
        (c["amount"] for c in cash_series if c["month"] == current_key), 0.0
    )
    forecast = compute_revenue_forecast(organization, months_ahead=1)
    forecast_next = forecast[0]["forecast_cash"] if forecast else 0.0
    forecast_label = forecast[0]["label"] if forecast else ""
    return {
        "mrr_now_str": _fmt_brl(kpis["mrr_now"]),
        "mrr_delta_pct": round(kpis["mrr_delta_pct"], 1),
        "mrr_delta_positive": kpis["mrr_delta_pct"] >= 0,
        "churn_pct": round(kpis["churn_pct"], 2),
        "new_this_month": kpis["new_this_month"],
        "canceled_this_month": kpis["canceled_this_month"],
        "net_adds": kpis["new_this_month"] - kpis["canceled_this_month"],
        "cash_month_str": _fmt_brl(cash_month),
        "delinquency_str": _fmt_brl(kpis["delinquency_amount"]),
        "delinquency_pct_of_mrr": round(kpis["delinquency_pct_of_mrr"], 1),
        "forecast_next_str": _fmt_brl(forecast_next),
        "forecast_label": forecast_label,
    }


def _build_collections_block(organization: Organization) -> dict[str, Any]:
    """Bloco de cobranças do digest semanal: foco em inadimplência crítica (90+)."""
    aging = compute_aging_distribution(organization)
    over_90 = next((b for b in aging if b["key"] == "OVER_90"), {})
    total_delinquency = sum(b["amount"] for b in aging if b["key"] != "ON_TIME")
    return {
        "over_90_str": _fmt_brl(over_90.get("amount", 0)),
        "over_90_count": over_90.get("count", 0),
        "total_delinquency_str": _fmt_brl(total_delinquency),
    }


@allow_cross_tenant(reason="digest de churn roda em Celery, escopo é a org passada")
def build_digest(organization: Organization, period: str) -> dict[str, Any]:
    """Monta o payload do digest da org para o período.

    Conteúdo diferenciado por período (#44): o mensal carrega um bloco
    estratégico (MRR, churn, net adds, caixa, inadimplência, forecast); o
    semanal carrega um bloco operacional de cobranças (inadimplência crítica).
    Ambos mantêm o panorama de risco de churn.
    """
    summary = compute_churn_risk_summary(organization)
    top = compute_top_risk_customers(organization, limit=_TOP_LIMIT[period])
    context: dict[str, Any] = {
        "organization": organization,
        "period": period,
        "period_label": _PERIOD_LABEL[period],
        "is_monthly": period == PERIOD_MONTHLY,
        "summary": summary,
        "revenue_at_risk_str": _fmt_brl(summary.get("revenue_at_risk")),
        "top": top,
        "generated_at": timezone.now(),
    }
    if period == PERIOD_MONTHLY:
        context["strategic"] = _build_strategic_block(organization)
    else:
        context["collections"] = _build_collections_block(organization)
    return context


def _recipients(organization: Organization, period: str) -> list[User]:
    optin = _OPTIN_FIELD[period]
    return list(
        User.objects.filter(
            is_active=True,
            memberships__organization=organization,
            memberships__is_active=True,
            **{optin: True},
        )
        .exclude(email="")
        .distinct()
    )


@allow_cross_tenant(reason="digest de churn roda em Celery, escopo é a org passada")
def send_churn_digest(organization: Organization, period: str) -> dict[str, Any]:
    """Renderiza e envia o digest aos usuários opt-in da org.

    Retorna {sent, recipients}. Se ninguém optou, não envia nada.
    """
    if period not in _PERIOD_LABEL:
        raise ValueError(f"período inválido: {period}")

    recipients = _recipients(organization, period)
    if not recipients:
        return {"sent": 0, "recipients": 0}

    context = build_digest(organization, period)
    if period == PERIOD_MONTHLY:
        subject = f"[Velus] Resumo estratégico mensal · {organization.name}"
    else:
        subject = f"[Velus] Foco da semana — risco e cobranças · {organization.name}"
    text_body = render_to_string("analytics/emails/churn_digest.txt", context)
    html_body = render_to_string("analytics/emails/churn_digest.html", context)

    sent = 0
    for user in recipients:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        msg.attach_alternative(html_body, "text/html")
        sent += msg.send()

    _logger.info(
        "churn_digest_sent",
        org=organization.slug,
        period=period,
        sent=sent,
        recipients=len(recipients),
    )
    return {"sent": sent, "recipients": len(recipients)}
