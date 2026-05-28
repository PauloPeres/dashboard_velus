"""Re-export pra Django descobrir os models."""

from __future__ import annotations

from .infrastructure.models import (
    DimContract,
    DimCustomer,
    DimPlan,
    FactContractStatusDaily,
    FactInvoice,
    FactPayment,
)

__all__ = (
    "DimContract",
    "DimCustomer",
    "DimPlan",
    "FactContractStatusDaily",
    "FactInvoice",
    "FactPayment",
)
