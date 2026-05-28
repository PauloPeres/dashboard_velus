"""AppConfig do bounded context Financial."""

from __future__ import annotations

from django.apps import AppConfig


class FinancialConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.financial"
    label = "financial"
    verbose_name = "Financial — faturas, pagamentos, inadimplência"
