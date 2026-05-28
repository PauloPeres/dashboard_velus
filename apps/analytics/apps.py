"""AppConfig do bounded context Analytics."""

from __future__ import annotations

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.analytics"
    label = "analytics"
    verbose_name = "Analytics — fact tables, MRR, churn"

    def ready(self) -> None:
        # Hook signal sync_completed → recompute fact tables incremental
        from . import signals  # noqa: F401
