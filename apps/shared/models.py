"""Modelos abstratos do kernel — TenantModel é a base de toda entidade de domínio."""

from __future__ import annotations

from django.db import models

from .managers import TenantManager
from .mixins import TimestampedMixin


class TenantModel(TimestampedMixin):
    """Base abstrata pra toda entidade de domínio multi-tenant.

    Garante:
    - Campo `organization` indexado, com `on_delete=PROTECT` (deletar org com
      dados é operação explícita, nunca cascata).
    - Manager `objects` que filtra por org do contexto automaticamente
      (ver TenantManager).
    - Timestamps (created_at, updated_at).

    Subclasses concretas devem definir seus próprios campos e Meta.indexes
    incluindo `organization` em queries comuns:

        class Meta:
            indexes = [
                models.Index(fields=["organization", "external_id"]),
                models.Index(fields=["organization", "document"]),
            ]
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.PROTECT,
        db_index=True,
        related_name="+",
        verbose_name="Organização",
    )

    objects = TenantManager()

    class Meta:
        abstract = True
