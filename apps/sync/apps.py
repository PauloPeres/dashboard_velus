"""AppConfig do bounded context Sync (orquestração)."""

from __future__ import annotations

from django.apps import AppConfig


class SyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sync"
    label = "sync"
    verbose_name = "Sync — orquestração"
