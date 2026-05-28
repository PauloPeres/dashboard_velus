"""Celery application — discovery automático de tasks via `autodiscover_tasks`."""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("velus_dashboard")

# Lê CELERY_* de django.conf.settings
app.config_from_object("django.conf:settings", namespace="CELERY")

# Procura `tasks.py` em todos os INSTALLED_APPS
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> None:
    """Task de debug — use `celery -A config call config.celery.debug_task`."""
    print(f"Request: {self.request!r}")
