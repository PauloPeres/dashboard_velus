"""Celery application — discovery automático de tasks via `autodiscover_tasks`."""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

app = Celery("velus_dashboard")

# Lê CELERY_* de django.conf.settings
app.config_from_object("django.conf:settings", namespace="CELERY")

# Procura `tasks.py` em todos os INSTALLED_APPS
app.autodiscover_tasks()


@task_prerun.connect
@task_postrun.connect
def _descarta_conexoes_quebradas(**_kwargs: object) -> None:
    """Uma task interrompida no meio de uma query não pode envenenar as seguintes.

    Antes e depois de cada task, para valer também pra conexão que já chegou
    quebrada (ver `apps.shared.db.descarta_conexoes_quebradas`).
    """
    from apps.shared.db import descarta_conexoes_quebradas

    descarta_conexoes_quebradas()


@app.task(bind=True, ignore_result=True)
def debug_task(self) -> None:
    """Task de debug — use `celery -A config call config.celery.debug_task`."""
    print(f"Request: {self.request!r}")
