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
    compute_churn_risk_summary,
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


@allow_cross_tenant(reason="digest de churn roda em Celery, escopo é a org passada")
def build_digest(organization: Organization, period: str) -> dict[str, Any]:
    """Monta o payload do digest da org para o período."""
    summary = compute_churn_risk_summary(organization)
    top = compute_top_risk_customers(organization, limit=_TOP_LIMIT[period])
    return {
        "organization": organization,
        "period": period,
        "period_label": _PERIOD_LABEL[period],
        "summary": summary,
        "revenue_at_risk_str": _fmt_brl(summary.get("revenue_at_risk")),
        "top": top,
        "generated_at": timezone.now(),
    }


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
    subject = (
        f"[Velus] Risco de churn — digest {context['period_label']} · "
        f"{organization.name}"
    )
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
