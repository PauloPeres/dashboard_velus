"""AppConfig de Scenarios."""

from __future__ import annotations

from django.apps import AppConfig


class ScenariosConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scenarios"
    label = "scenarios"
    verbose_name = "Scenarios — simuladores financeiros"
