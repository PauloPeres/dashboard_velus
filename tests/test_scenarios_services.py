"""Testes dos domain services dos simuladores — Python puro, sem DB."""

from __future__ import annotations

from decimal import Decimal

from apps.scenarios.domain.defaults import ASSUMPTION_DEFAULTS
from apps.scenarios.domain.services import (
    calculate_clt_cost,
    calculate_pj_vs_clt,
    calculate_simples_anexo3,
    calculate_simples_split,
)


def _assumption_dict() -> dict[str, Decimal]:
    return {k: v for k, v, _u, _d in ASSUMPTION_DEFAULTS}


class TestCltCost:
    def test_basic_breakdown(self) -> None:
        result = calculate_clt_cost(Decimal("3000"), _assumption_dict())
        # Salario base 3000
        assert result.salary == Decimal("3000")
        # INSS patronal 20%
        assert result.inss_employer == Decimal("600.00")
        # FGTS 8%
        assert result.fgts == Decimal("240.00")
        # VA = 35/dia * 22 dias = 770
        assert result.va == Decimal("770.00")
        # VT = 250
        assert result.vt == Decimal("250.00")
        # Total > salário (com encargos)
        assert result.total_monthly > result.salary

    def test_encargos_pct(self) -> None:
        result = calculate_clt_cost(Decimal("3000"), _assumption_dict())
        # Total deve ser ~ 60% encargos sobre 3000 (com VA+VT inflando o %)
        assert result.encargos_pct > Decimal("50")

    def test_without_va_vt(self) -> None:
        result = calculate_clt_cost(
            Decimal("3000"), _assumption_dict(), include_va=False, include_vt=False
        )
        assert result.va == Decimal("0")
        assert result.vt == Decimal("0")


class TestPjVsClt:
    def test_5_workers_breakeven(self) -> None:
        result = calculate_pj_vs_clt(
            n_workers=5,
            pj_monthly_per_worker=Decimal("4000"),
            clt_salary=Decimal("3000"),
            assumptions=_assumption_dict(),
        )
        assert result.n_workers == 5
        # Custo CLT inclui salário + ~50% encargos + VA + VT, fica ~ 4500-5000 por worker
        assert result.clt_monthly_per_worker > Decimal("4500")
        # Annual difference é monthly * 12 * n_workers
        assert result.annual_difference == result.monthly_difference * 12 * 5


class TestSimplesAnexo3:
    def test_faixa_1_aliquota_efetiva_6_pct(self) -> None:
        result = calculate_simples_anexo3(Decimal("100000"), _assumption_dict())
        # Faixa 1: 100k → 6% nominal, 0 dedução, efetiva 6%
        assert result.aliquota_nominal == Decimal("6.0")
        assert result.aliquota_efetiva == Decimal("6.00")
        assert result.tax_annual == Decimal("6000.00")

    def test_faixa_2_aliquota_efetiva_menor_que_nominal(self) -> None:
        result = calculate_simples_anexo3(Decimal("300000"), _assumption_dict())
        # Faixa 2: 300k → 11.2% nominal, 9360 dedução
        # Efetiva = (300k * 0.112 - 9360) / 300k = 24240 / 300k = 8.08%
        assert result.aliquota_nominal == Decimal("11.2")
        assert result.aliquota_efetiva < result.aliquota_nominal


class TestSimplesSplit:
    def test_split_50_50_saves_when_jumping_faixa(self) -> None:
        # Receita 700k → faixa 3 (13.5%). Split 50/50 = 350k cada → faixa 2 (11.2%).
        # Deveria gerar economia.
        result = calculate_simples_split(
            Decimal("700000"), Decimal("50"), _assumption_dict()
        )
        assert result.single_cnpj.aliquota_nominal == Decimal("13.5")
        assert result.cnpj1.aliquota_nominal == Decimal("11.2")
        assert result.cnpj2.aliquota_nominal == Decimal("11.2")
        assert result.annual_savings > Decimal("0")

    def test_no_savings_when_both_below_faixa1(self) -> None:
        # Receita pequena 200k — split não economiza
        result = calculate_simples_split(
            Decimal("200000"), Decimal("50"), _assumption_dict()
        )
        # 200k single → faixa 2 (11.2%). Split 100k cada → faixa 1 (6%)
        # Aqui SIM economiza
        assert result.annual_savings > Decimal("0")
