"""Domain services dos simuladores — Python puro, sem Django ORM.

Cada função recebe `assumptions: dict[str, Decimal]` (carregado fora) e
parâmetros do cenário, retorna resultado calculado. Testáveis isoladamente.

NOTA: encargos e alíquotas são MODELO simplificado. Validar com contador
antes de usar pra decisão real.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


# =============================================================================
# Tipos de retorno
# =============================================================================
@dataclass(frozen=True)
class CltCostBreakdown:
    salary: Decimal
    inss_employer: Decimal
    fgts: Decimal
    rat: Decimal
    terceiros: Decimal
    prov_13o: Decimal
    prov_ferias: Decimal
    prov_rescisao: Decimal
    va: Decimal
    vt: Decimal

    @property
    def total_monthly(self) -> Decimal:
        return (
            self.salary + self.inss_employer + self.fgts + self.rat + self.terceiros
            + self.prov_13o + self.prov_ferias + self.prov_rescisao
            + self.va + self.vt
        )

    @property
    def total_annual(self) -> Decimal:
        return self.total_monthly * 12

    @property
    def encargos_pct(self) -> Decimal:
        """% dos encargos sobre salário base."""
        if self.salary == 0:
            return Decimal("0")
        return ((self.total_monthly - self.salary) / self.salary * 100).quantize(Decimal("0.01"))

    def as_dict(self) -> dict[str, float]:
        return {
            "salary": float(self.salary),
            "inss_employer": float(self.inss_employer),
            "fgts": float(self.fgts),
            "rat": float(self.rat),
            "terceiros": float(self.terceiros),
            "prov_13o": float(self.prov_13o),
            "prov_ferias": float(self.prov_ferias),
            "prov_rescisao": float(self.prov_rescisao),
            "va": float(self.va),
            "vt": float(self.vt),
            "total_monthly": float(self.total_monthly),
            "total_annual": float(self.total_annual),
            "encargos_pct": float(self.encargos_pct),
        }


# =============================================================================
# CLT cost calculator
# =============================================================================
def _pct(assumptions: dict[str, Decimal], key: str) -> Decimal:
    """Converte assumption em % pra fator decimal (5% → 0.05)."""
    return assumptions[key] / Decimal("100")


def calculate_clt_cost(
    salary: Decimal,
    assumptions: dict[str, Decimal],
    *,
    include_va: bool = True,
    include_vt: bool = True,
) -> CltCostBreakdown:
    """Custo total CLT mensal (salário + encargos + provisões + benefícios)."""
    inss = (salary * _pct(assumptions, "clt_inss_employer_pct")).quantize(Decimal("0.01"))
    fgts = (salary * _pct(assumptions, "clt_fgts_pct")).quantize(Decimal("0.01"))
    rat = (salary * _pct(assumptions, "clt_rat_pct")).quantize(Decimal("0.01"))
    terceiros = (salary * _pct(assumptions, "clt_terceiros_pct")).quantize(Decimal("0.01"))
    prov_13o = (salary * _pct(assumptions, "clt_provision_13o_pct")).quantize(Decimal("0.01"))
    prov_ferias = (salary * _pct(assumptions, "clt_provision_ferias_pct")).quantize(Decimal("0.01"))
    prov_rescisao = (salary * _pct(assumptions, "clt_provision_rescisao_pct")).quantize(Decimal("0.01"))

    va = Decimal("0")
    if include_va:
        va = (assumptions["clt_va_per_day"] * assumptions["clt_days_per_month"]).quantize(Decimal("0.01"))

    vt = assumptions["clt_vt_per_month"].quantize(Decimal("0.01")) if include_vt else Decimal("0")

    return CltCostBreakdown(
        salary=salary,
        inss_employer=inss,
        fgts=fgts,
        rat=rat,
        terceiros=terceiros,
        prov_13o=prov_13o,
        prov_ferias=prov_ferias,
        prov_rescisao=prov_rescisao,
        va=va,
        vt=vt,
    )


# =============================================================================
# PJ vs CLT
# =============================================================================
@dataclass(frozen=True)
class PjVsCltResult:
    n_workers: int
    clt_monthly_per_worker: Decimal
    clt_annual_total: Decimal
    pj_monthly_per_worker: Decimal
    pj_annual_total: Decimal
    monthly_difference: Decimal  # positivo = CLT mais caro; negativo = CLT mais barato
    annual_difference: Decimal
    clt_breakdown: CltCostBreakdown

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_workers": self.n_workers,
            "clt_monthly_per_worker": float(self.clt_monthly_per_worker),
            "clt_annual_total": float(self.clt_annual_total),
            "pj_monthly_per_worker": float(self.pj_monthly_per_worker),
            "pj_annual_total": float(self.pj_annual_total),
            "monthly_difference": float(self.monthly_difference),
            "annual_difference": float(self.annual_difference),
            "clt_breakdown": self.clt_breakdown.as_dict(),
        }


def calculate_pj_vs_clt(
    *,
    n_workers: int,
    pj_monthly_per_worker: Decimal,
    clt_salary: Decimal,
    assumptions: dict[str, Decimal],
) -> PjVsCltResult:
    """Compara custo CLT vs PJ para N técnicos.

    CLT inclui encargos + provisões + VA + VT (benefícios sindicato ESP).
    PJ é só o pagamento mensal informado (assumido já com tudo embutido).
    """
    breakdown = calculate_clt_cost(clt_salary, assumptions)
    clt_monthly_per_worker = breakdown.total_monthly
    clt_annual = clt_monthly_per_worker * 12 * n_workers
    pj_annual = pj_monthly_per_worker * 12 * n_workers
    return PjVsCltResult(
        n_workers=n_workers,
        clt_monthly_per_worker=clt_monthly_per_worker,
        clt_annual_total=clt_annual,
        pj_monthly_per_worker=pj_monthly_per_worker,
        pj_annual_total=pj_annual,
        monthly_difference=clt_monthly_per_worker - pj_monthly_per_worker,
        annual_difference=clt_annual - pj_annual,
        clt_breakdown=breakdown,
    )


# =============================================================================
# Simples Nacional — Anexo III (ISP típico)
# =============================================================================
@dataclass(frozen=True)
class SimplesCalc:
    revenue_12m: Decimal
    aliquota_nominal: Decimal  # % faixa
    deducao: Decimal  # R$ a deduzir
    aliquota_efetiva: Decimal  # % real
    tax_annual: Decimal  # R$ devido no ano

    def as_dict(self) -> dict[str, float]:
        return {
            "revenue_12m": float(self.revenue_12m),
            "aliquota_nominal": float(self.aliquota_nominal),
            "deducao": float(self.deducao),
            "aliquota_efetiva": float(self.aliquota_efetiva),
            "tax_annual": float(self.tax_annual),
        }


def calculate_simples_anexo3(
    revenue_12m: Decimal, assumptions: dict[str, Decimal]
) -> SimplesCalc:
    """Calcula alíquota efetiva Simples Anexo III pra uma receita anual."""
    faixas = [
        (assumptions["simples_anexo3_limite_1"], assumptions["simples_anexo3_aliq_1"], Decimal("0")),
        (assumptions["simples_anexo3_limite_2"], assumptions["simples_anexo3_aliq_2"], assumptions["simples_anexo3_dedu_2"]),
        (assumptions["simples_anexo3_limite_3"], assumptions["simples_anexo3_aliq_3"], assumptions["simples_anexo3_dedu_3"]),
        (assumptions["simples_anexo3_limite_4"], assumptions["simples_anexo3_aliq_4"], assumptions["simples_anexo3_dedu_4"]),
        (assumptions["simples_anexo3_limite_5"], assumptions["simples_anexo3_aliq_5"], assumptions["simples_anexo3_dedu_5"]),
        (assumptions["simples_anexo3_limite_6"], assumptions["simples_anexo3_aliq_6"], assumptions["simples_anexo3_dedu_6"]),
    ]

    aliq_nom = Decimal("0")
    deducao = Decimal("0")
    for limite, aliq, dedu in faixas:
        if revenue_12m <= limite:
            aliq_nom = aliq
            deducao = dedu
            break
    else:
        # Acima do limite máximo — sai do Simples; alíquota teórica do lucro presumido ~16-21%
        aliq_nom = Decimal("16")
        deducao = Decimal("0")

    aliq_efetiva = ((revenue_12m * (aliq_nom / 100) - deducao) / revenue_12m * 100) if revenue_12m > 0 else Decimal("0")
    aliq_efetiva = aliq_efetiva.quantize(Decimal("0.01"))
    tax_annual = (revenue_12m * (aliq_efetiva / 100)).quantize(Decimal("0.01"))

    return SimplesCalc(
        revenue_12m=revenue_12m,
        aliquota_nominal=aliq_nom,
        deducao=deducao,
        aliquota_efetiva=aliq_efetiva,
        tax_annual=tax_annual,
    )


@dataclass(frozen=True)
class SimplesSplitResult:
    total_revenue: Decimal
    split_pct: Decimal  # 0-100; quanto do faturamento vai pro CNPJ 2
    cnpj1: SimplesCalc
    cnpj2: SimplesCalc
    single_cnpj: SimplesCalc
    annual_savings: Decimal  # tax_single - (tax_cnpj1 + tax_cnpj2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_revenue": float(self.total_revenue),
            "split_pct": float(self.split_pct),
            "cnpj1": self.cnpj1.as_dict(),
            "cnpj2": self.cnpj2.as_dict(),
            "single_cnpj": self.single_cnpj.as_dict(),
            "annual_savings": float(self.annual_savings),
        }


def calculate_simples_split(
    total_revenue: Decimal,
    split_pct: Decimal,
    assumptions: dict[str, Decimal],
) -> SimplesSplitResult:
    """Compara 1 CNPJ vs split em 2 CNPJs no Simples Nacional."""
    rev2 = (total_revenue * split_pct / 100).quantize(Decimal("0.01"))
    rev1 = total_revenue - rev2

    cnpj1 = calculate_simples_anexo3(rev1, assumptions)
    cnpj2 = calculate_simples_anexo3(rev2, assumptions)
    single = calculate_simples_anexo3(total_revenue, assumptions)

    savings = single.tax_annual - (cnpj1.tax_annual + cnpj2.tax_annual)

    return SimplesSplitResult(
        total_revenue=total_revenue,
        split_pct=split_pct,
        cnpj1=cnpj1,
        cnpj2=cnpj2,
        single_cnpj=single,
        annual_savings=savings,
    )


# =============================================================================
# Helper — load assumptions from DB
# =============================================================================
def load_assumptions(organization: Any) -> dict[str, Decimal]:
    """Carrega premissas atuais de uma org em dict pronto pros services."""
    from apps.scenarios.infrastructure.models import Assumption

    return {a.key: a.value for a in Assumption.objects.filter(organization=organization)}
