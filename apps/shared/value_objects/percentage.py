"""Percentage value object — fração 0..1 com helpers de conversão."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .money import Money


@dataclass(frozen=True)
class Percentage:
    """Percentual armazenado como fração (0..1).

    Convenção: `Percentage(Decimal("0.05"))` = 5%.

    Métodos `.of(money)` aplicam o percentual a um Money — útil pra
    juros, descontos, alíquotas em simuladores.
    """

    value: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.value, Decimal):
            object.__setattr__(self, "value", Decimal(str(self.value)))

    def of(self, money: Money) -> Money:
        """Calcula `self * money` (R$ X * 5% = R$ X * 0.05)."""
        return Money(money.amount * self.value, money.currency)

    def __str__(self) -> str:
        return f"{(self.value * 100):.2f}%"

    def __add__(self, other: Percentage) -> Percentage:
        return Percentage(self.value + other.value)

    def __sub__(self, other: Percentage) -> Percentage:
        return Percentage(self.value - other.value)

    def __mul__(self, factor: Decimal | int | float) -> Percentage:
        return Percentage(self.value * Decimal(str(factor)))

    @classmethod
    def from_percent(cls, percent: Decimal | float | int) -> Percentage:
        """Cria a partir de inteiro/decimal em formato `100%` (ex.: 5 = 5%, 70 = 70%)."""
        return cls(Decimal(str(percent)) / Decimal("100"))

    @classmethod
    def zero(cls) -> Percentage:
        return cls(Decimal("0"))
