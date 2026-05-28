"""Models de Scenarios.

`Assumption` é um catálogo de premissas (% encargos CLT, alíquotas Simples, etc.)
editáveis por org via admin. Default vem de data migration.

`Scenario` é um cenário salvo (PJ vs CLT específico, ajuste salarial X, etc.)
serializando inputs e resultados pra revisitar depois.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.shared.models import TenantModel


class Assumption(TenantModel):
    """Premissa numerica editável — usada por todos os simuladores."""

    key = models.CharField(max_length=64)
    value = models.DecimalField(max_digits=14, decimal_places=6)
    description = models.TextField(blank=True, default="")
    unit = models.CharField(max_length=16, default="%")  # % | R$ | dias | x

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Premissa")
        verbose_name_plural = _("Premissas")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "key"],
                name="unique_assumption_per_org",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "key"]),
        ]

    def __str__(self) -> str:
        return f"{self.key} = {self.value} {self.unit}"


class Scenario(TenantModel):
    """Cenário salvo de um simulador específico."""

    class Type(models.TextChoices):
        PJ_VS_CLT = "PJ_VS_CLT", _("PJ vs CLT")
        SALARY_ADJUST = "SALARY_ADJUST", _("Ajuste salarial")
        UNION_ESP = "UNION_ESP", _("Sindicato ESP")
        SIMPLES_SPLIT = "SIMPLES_SPLIT", _("Split CNPJ — Simples Nacional")

    type = models.CharField(max_length=24, choices=Type.choices)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")

    # Inputs do simulador (JSON, formato depende do type)
    inputs = models.JSONField(default=dict)
    # Resultados calculados — snapshot pro user revisitar sem rerodar
    results = models.JSONField(default=dict)

    # Snapshot da data dos cálculos
    base_at = models.DateTimeField()

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Cenário")
        verbose_name_plural = _("Cenários")
        indexes = [
            models.Index(fields=["organization", "type", "-base_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()} · {self.name}"
