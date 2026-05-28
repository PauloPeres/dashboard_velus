"""Testes dos Value Objects — Python puro, sem DB."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.shared.value_objects import Money, Percentage


class TestMoney:
    def test_addition_same_currency(self) -> None:
        a = Money(Decimal("100"))
        b = Money(Decimal("50.50"))
        assert (a + b).amount == Decimal("150.50")

    def test_addition_different_currency_raises(self) -> None:
        a = Money(Decimal("100"), "BRL")
        b = Money(Decimal("50"), "USD")
        with pytest.raises(ValueError, match="moedas diferentes"):
            a + b

    def test_centavos_roundtrip(self) -> None:
        m = Money.from_centavos(15050)
        assert m.amount == Decimal("150.50")
        assert m.to_centavos() == 15050

    def test_multiplication(self) -> None:
        m = Money(Decimal("100"))
        result = m * Decimal("1.5")
        assert result.amount == Decimal("150.00")

    def test_normalization_to_two_decimals(self) -> None:
        m = Money(Decimal("100.99999"))
        assert m.amount == Decimal("101.00")  # ROUND_HALF_EVEN

    def test_comparison(self) -> None:
        a = Money(Decimal("100"))
        b = Money(Decimal("200"))
        assert a < b
        assert b > a
        assert a <= a
        assert a >= a


class TestPercentage:
    def test_apply_to_money(self) -> None:
        p = Percentage.from_percent(5)  # 5%
        m = Money(Decimal("1000"))
        assert p.of(m) == Money(Decimal("50.00"))

    def test_from_percent(self) -> None:
        p = Percentage.from_percent(70)
        assert p.value == Decimal("0.7")

    def test_str_format(self) -> None:
        p = Percentage(Decimal("0.075"))
        assert str(p) == "7.50%"

    def test_addition(self) -> None:
        a = Percentage.from_percent(5)
        b = Percentage.from_percent(3)
        c = a + b
        assert c.value == Decimal("0.08")
