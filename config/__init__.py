"""Configuração do projeto Velus Dashboard.

Garante que o Celery app está carregado quando Django sobe — necessário
para `@shared_task` em apps funcionar.
"""

from __future__ import annotations

from .celery import app as celery_app

__all__ = ("celery_app",)
