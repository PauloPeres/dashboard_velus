"""Value Objects do kernel — imutáveis, sem ID, comparados por valor."""

from __future__ import annotations

from .money import Money
from .percentage import Percentage

__all__ = ("Money", "Percentage")
