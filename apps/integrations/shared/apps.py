"""AppConfig da infraestrutura compartilhada de integrações."""

from __future__ import annotations

from django.apps import AppConfig


class IntegrationsSharedConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations.shared"
    label = "integrations_shared"
    verbose_name = "Integrações: infra compartilhada"
