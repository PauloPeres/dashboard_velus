"""Defaults de premissas pra cada Organization recém-criada.

Valores conservadores baseados em médias de mercado brasileiro 2025/2026
para ISPs pequenos. Editáveis via admin por tenant — TODO: contador real
deve refinar pra realidade da Velus.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

# (key, value, unit, description)
ASSUMPTION_DEFAULTS: list[tuple[str, Decimal, str, str]] = [
    # -------------------------------------------------------------------------
    # Encargos CLT — base salário
    # -------------------------------------------------------------------------
    (
        "clt_inss_employer_pct",
        Decimal("20.0"),
        "%",
        "INSS patronal — 20% sobre folha (Anexo I do Simples = isento; outros = 20%)",
    ),
    (
        "clt_fgts_pct",
        Decimal("8.0"),
        "%",
        "FGTS — 8% sobre salário e adicional",
    ),
    (
        "clt_rat_pct",
        Decimal("3.0"),
        "%",
        "RAT/SAT — risco ambiental, ISP geralmente 3% (atividade grau médio)",
    ),
    (
        "clt_terceiros_pct",
        Decimal("5.8"),
        "%",
        "Terceiros (SESI/SENAI/INCRA/Salário Educação/SEBRAE) — varia 5.2-5.8%",
    ),
    # -------------------------------------------------------------------------
    # Provisões anuais (rateio mensal)
    # -------------------------------------------------------------------------
    (
        "clt_provision_13o_pct",
        Decimal("8.33"),
        "%",
        "13º salário = 1/12 do salário ao mês",
    ),
    (
        "clt_provision_ferias_pct",
        Decimal("11.11"),
        "%",
        "Férias (1/12) + 1/3 constitucional = 11.11% sobre salário",
    ),
    (
        "clt_provision_rescisao_pct",
        Decimal("4.0"),
        "%",
        "Provisão pra rescisão (multa FGTS 40% × 1/12 anual)",
    ),
    # -------------------------------------------------------------------------
    # Benefícios obrigatórios (sindicato ESP)
    # -------------------------------------------------------------------------
    (
        "clt_va_per_day",
        Decimal("35.00"),
        "R$",
        "Vale alimentação por dia útil — base sindicato ESP",
    ),
    (
        "clt_vt_per_month",
        Decimal("250.00"),
        "R$",
        "Vale transporte mensal (descontado 6% do salário)",
    ),
    (
        "clt_days_per_month",
        Decimal("22"),
        "dias",
        "Média de dias úteis por mês",
    ),
    # -------------------------------------------------------------------------
    # Simples Nacional — alíquotas Anexo III (serviços)
    # ISPs em geral aplicam Anexo III; consulte contador real.
    # Faixas em R$ anuais, alíquota efetiva e parcela a deduzir.
    # -------------------------------------------------------------------------
    (
        "simples_anexo3_limite_1",
        Decimal("180000"),
        "R$",
        "Faixa 1 do Anexo III: até R$ 180.000 anual → 6%",
    ),
    (
        "simples_anexo3_aliq_1",
        Decimal("6.0"),
        "%",
        "Alíquota faixa 1 do Anexo III",
    ),
    (
        "simples_anexo3_limite_2",
        Decimal("360000"),
        "R$",
        "Faixa 2: até R$ 360.000 → 11.20% (deduz R$ 9.360)",
    ),
    (
        "simples_anexo3_aliq_2",
        Decimal("11.2"),
        "%",
        "Alíquota faixa 2",
    ),
    (
        "simples_anexo3_dedu_2",
        Decimal("9360"),
        "R$",
        "Parcela a deduzir faixa 2",
    ),
    (
        "simples_anexo3_limite_3",
        Decimal("720000"),
        "R$",
        "Faixa 3: até R$ 720.000 → 13.5% (deduz R$ 17.640)",
    ),
    (
        "simples_anexo3_aliq_3",
        Decimal("13.5"),
        "%",
        "Alíquota faixa 3",
    ),
    (
        "simples_anexo3_dedu_3",
        Decimal("17640"),
        "R$",
        "Parcela a deduzir faixa 3",
    ),
    (
        "simples_anexo3_limite_4",
        Decimal("1800000"),
        "R$",
        "Faixa 4: até R$ 1.800.000 → 16% (deduz R$ 35.640)",
    ),
    (
        "simples_anexo3_aliq_4",
        Decimal("16.0"),
        "%",
        "Alíquota faixa 4",
    ),
    (
        "simples_anexo3_dedu_4",
        Decimal("35640"),
        "R$",
        "Parcela a deduzir faixa 4",
    ),
    (
        "simples_anexo3_limite_5",
        Decimal("3600000"),
        "R$",
        "Faixa 5: até R$ 3.600.000 → 21% (deduz R$ 125.640)",
    ),
    (
        "simples_anexo3_aliq_5",
        Decimal("21.0"),
        "%",
        "Alíquota faixa 5",
    ),
    (
        "simples_anexo3_dedu_5",
        Decimal("125640"),
        "R$",
        "Parcela a deduzir faixa 5",
    ),
    (
        "simples_anexo3_limite_6",
        Decimal("4800000"),
        "R$",
        "Faixa 6: até R$ 4.800.000 → 33% (deduz R$ 648.000)",
    ),
    (
        "simples_anexo3_aliq_6",
        Decimal("33.0"),
        "%",
        "Alíquota faixa 6 (último limite antes de sair do Simples)",
    ),
    (
        "simples_anexo3_dedu_6",
        Decimal("648000"),
        "R$",
        "Parcela a deduzir faixa 6",
    ),
]


def seed_defaults_for_organization(organization: Any) -> int:
    """Cria/atualiza premissas default pra uma org. Idempotente.

    Roda sob @allow_cross_tenant porque o caller (create_organization command)
    opera fora de request HTTP — escopo é o `organization` explícito.
    """
    from apps.scenarios.infrastructure.models import Assumption
    from apps.shared.decorators import allow_cross_tenant

    @allow_cross_tenant(reason="seed de premissas default pra nova org")
    def _do_seed() -> int:
        count = 0
        for key, value, unit, description in ASSUMPTION_DEFAULTS:
            _, created = Assumption.objects.update_or_create(
                organization=organization,
                key=key,
                defaults={"value": value, "unit": unit, "description": description},
            )
            if created:
                count += 1
        return count

    return _do_seed()
