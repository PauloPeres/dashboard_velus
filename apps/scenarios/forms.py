"""Forms dos simuladores."""

from __future__ import annotations

from decimal import Decimal

from django import forms


class PjVsCltForm(forms.Form):
    n_workers = forms.IntegerField(
        label="Número de técnicos", min_value=1, max_value=200, initial=5,
    )
    pj_monthly_per_worker = forms.DecimalField(
        label="Custo PJ médio mensal por técnico (R$)",
        min_value=Decimal("0"), max_digits=10, decimal_places=2, initial=Decimal("4000"),
    )
    clt_salary = forms.DecimalField(
        label="Salário CLT proposto (R$)",
        min_value=Decimal("0"), max_digits=10, decimal_places=2, initial=Decimal("3000"),
    )


class SimplesSplitForm(forms.Form):
    total_revenue = forms.DecimalField(
        label="Faturamento anual total (R$)",
        min_value=Decimal("0"), max_digits=12, decimal_places=2,
        initial=Decimal("3000000"),
    )
    split_pct = forms.DecimalField(
        label="% que vai pro 2º CNPJ",
        min_value=Decimal("0"), max_value=Decimal("100"),
        max_digits=5, decimal_places=2, initial=Decimal("50"),
    )


class SalaryAdjustForm(forms.Form):
    person_name = forms.CharField(label="Pessoa", max_length=128, initial="Luan")
    current_salary = forms.DecimalField(
        label="Salário atual (R$)",
        min_value=Decimal("0"), max_digits=10, decimal_places=2, initial=Decimal("4000"),
    )
    new_salary = forms.DecimalField(
        label="Salário proposto (R$)",
        min_value=Decimal("0"), max_digits=10, decimal_places=2, initial=Decimal("5500"),
    )


class UnionEspForm(forms.Form):
    va_per_day = forms.DecimalField(
        label="Vale alimentação por dia útil (R$)",
        min_value=Decimal("0"), max_digits=8, decimal_places=2, initial=Decimal("45"),
    )
    salary_adjust_pct = forms.DecimalField(
        label="% reajuste sobre folha",
        min_value=Decimal("-50"), max_value=Decimal("100"),
        max_digits=5, decimal_places=2, initial=Decimal("5"),
    )
    headcount = forms.IntegerField(
        label="Quantidade de CLT impactados", min_value=1, initial=3,
    )
    avg_salary = forms.DecimalField(
        label="Salário médio CLT (R$)",
        min_value=Decimal("0"), max_digits=10, decimal_places=2, initial=Decimal("3500"),
    )
