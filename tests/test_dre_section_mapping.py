"""Testes do mapeamento cod → seção do Fluxo de Caixa por Conta (#40).

`_get_dre_section` classifica contas do plano IXC em seções. Overrides por cod
completo (`_DRE_ACCOUNT_OVERRIDES`) corrigem contas cujo prefixo IXC não reflete
a natureza real — ex.: "Empréstimos e financiamentos" sob 5.1 (Comerciais) que
na verdade é despesa financeira.
"""

from __future__ import annotations

import pytest

from apps.analytics.application.aggregations import _get_dre_section


@pytest.mark.parametrize(
    ("cod", "section"),
    [
        # Override por cod completo: empréstimos/financiamentos → Financeiras
        ("5.1.03.0001", "Despesas Financeiras"),
        # Mesma sub-árvore 5.1.03 mas conta diferente NÃO é sobrescrita
        ("5.1.03.002", "Despesas Comerciais"),
        ("5.1", "Despesas Comerciais"),
        # Prefixos de dois segmentos
        ("5.2.01.002", "Despesas Operacionais"),
        ("5.3.01", "Despesas Financeiras"),
        ("5.4", "Outras Despesas"),
        # Fallback de um segmento
        ("4.1.02", "Custos dos Serviços"),
        ("4", "Custos dos Serviços"),
        ("5", "Outras Despesas"),
        # Imobilizado / caixa
        ("1.2.01", "Investimentos & Imobilizado"),
        ("1.1.01", "Movimentações de Caixa"),
        # Vazio → sem categoria
        ("", "Sem Categoria"),
    ],
)
def test_get_dre_section(cod: str, section: str) -> None:
    assert _get_dre_section(cod)[0] == section


def test_override_takes_priority_over_prefix() -> None:
    # Sem o override, "5.1.03.0001" cairia em Comerciais (prefixo 5.1).
    sec, order = _get_dre_section("5.1.03.0001")
    assert sec == "Despesas Financeiras"
    # ordem alinhada à seção financeira do mapa de prefixos
    assert order == _get_dre_section("5.3.01")[1]


def test_trailing_dot_is_normalized() -> None:
    # cods do IXC às vezes vêm com ponto final ("4.2.").
    assert _get_dre_section("4.2.")[0] == "Custos dos Serviços"
