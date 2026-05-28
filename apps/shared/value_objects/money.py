"""Money value object — Decimal seguro com currency."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal


@dataclass(frozen=True)
class Money:
    """Valor monetário imutável.

    - Armazena `amount` como Decimal (precisão exata, sem float).
    - `currency` default BRL — para Velus suficiente; preparado pra multi-currency
      se algum tenant futuro operar em USD.
    - Operações entre moedas diferentes levantam ValueError.

    Não persiste diretamente em DB — converta para `DecimalField(max_digits=14, decimal_places=2)`.
    """

    amount: Decimal
    currency: str = "BRL"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
        # Normaliza pra 2 casas decimais (centavo)
        normalized = self.amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        object.__setattr__(self, "amount", normalized)

    def __add__(self, other: Money) -> Money:
        self._ensure_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._ensure_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: Decimal | int | float) -> Money:
        return Money(self.amount * Decimal(str(factor)), self.currency)

    def __truediv__(self, divisor: Decimal | int | float) -> Money:
        return Money(self.amount / Decimal(str(divisor)), self.currency)

    def __lt__(self, other: Money) -> bool:
        self._ensure_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._ensure_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._ensure_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._ensure_same_currency(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.currency} {self.amount:,.2f}"

    def _ensure_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"Operação entre moedas diferentes: {self.currency} vs {other.currency}"
            )

    @classmethod
    def zero(cls, currency: str = "BRL") -> Money:
        return cls(Decimal("0"), currency)

    @classmethod
    def from_centavos(cls, centavos: int, currency: str = "BRL") -> Money:
        """Converte de int em centavos (formato comum em APIs financeiras)."""
        return cls(Decimal(centavos) / Decimal("100"), currency)

    def to_centavos(self) -> int:
        """Converte pra int em centavos."""
        return int((self.amount * 100).to_integral_value())
