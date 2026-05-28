"""Mixins reutilizáveis para models."""

from __future__ import annotations

from django.conf import settings
from django.db import models


class TimestampedMixin(models.Model):
    """Adiciona created_at e updated_at automáticos."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AuditedMixin(models.Model):
    """Adiciona created_by e updated_by — quem fez a operação.

    Setado automaticamente em views via mixin de view (a definir);
    Em management commands precisa ser preenchido manualmente.
    """

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",  # sem reverse — auditoria não navega de User
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        abstract = True
