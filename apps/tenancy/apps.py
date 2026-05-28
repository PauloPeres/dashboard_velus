"""AppConfig do bounded context de tenancy."""

from __future__ import annotations

from django.apps import AppConfig


class TenancyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenancy"
    label = "tenancy"
    verbose_name = "Tenancy & Identity"
