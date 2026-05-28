"""AppConfig do kernel compartilhado."""

from __future__ import annotations

from django.apps import AppConfig


class SharedConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.shared"
    label = "shared"
    verbose_name = "Kernel compartilhado"
