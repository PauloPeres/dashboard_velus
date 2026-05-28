"""AppConfig do bounded context de Customers."""

from __future__ import annotations

from django.apps import AppConfig


class CustomersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.customers"
    label = "customers"
    verbose_name = "Customers"
