"""Backtest dos sinais de risco de churn — issue #125.

Responde, com número em vez de opinião: *quem tinha o sinal X numa data passada
cancelou mais do que quem não tinha?*

O método é sempre o mesmo:

1. **Base em D0** — contratos ativados até D0 e não cancelados até D0.
   Reconstruída de `Contract`, nunca do `FactContractStatusDaily`: até a #122
   aquele fact projetava o presente pra trás, e mesmo corrigido ele só tem
   status histórico real a partir de ago/2026.
2. **Desfecho** — cancelou entre D0 e D0+horizonte, por `canceled_at`.
3. **Comparação** — taxa de cancelamento de quem tinha o sinal contra quem não
   tinha. O `lift` é a razão entre as duas: 1,0 é o acaso; abaixo de 1,0 o sinal
   aponta pro lado errado.

**Só entram sinais com história real.** Chamados (`Ticket.opened_at`) e atraso
(`Invoice.due_date`/`paid_at`) têm data e são reconstrutíveis. Bloqueio
prolongado e downgrade dependem de status/valor histórico de contrato, que só
existe a partir do `simple_history` de ago/2026 — reconstruí-los hoje seria
inventar, que é o erro que a #122 corrigiu.

Quando houver `FactChurnRiskDaily` na data (#123), o backtest avalia também o
**score que o modelo realmente atribuiu** naquele dia — que é a validação de
verdade, sem reconstrução nenhuma.

Tudo somente leitura.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import Q

from apps.analytics.infrastructure.models import FactChurnRiskDaily
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

# Janelas dos sinais reconstruíveis — espelham `churn_risk.py`.
TICKETS_WINDOW_DAYS = 30
TICKETS_MIN = 3
LATE_PAYMENTS_WINDOW_DAYS = 180
LATE_PAYMENTS_MIN = 3


@dataclass(frozen=True)
class ResultadoSinal:
    """Uma linha do relatório: como o sinal separou quem cancelou de quem não."""

    nome: str
    n: int
    churn_no_grupo: float
    churn_fora: float
    lift: float
    cobertura: float
    cancelados_no_grupo: int

    @property
    def separa(self) -> bool:
        """O sinal aponta pro lado certo? Abaixo de 1,0 aponta pro lado errado."""
        return self.lift > 1.0


@dataclass(frozen=True)
class Backtest:
    d0: date
    horizonte: int
    base: int
    cancelados: int
    taxa_base: float
    sinais: list[ResultadoSinal]


def _avalia(
    nome: str, grupo: set[int], base: set[int], saiu: set[int], minimo: int = 15
) -> ResultadoSinal | None:
    """Compara o grupo com o resto da base. None quando a amostra é pequena demais.

    O corte de amostra existe porque lift de n=5 é anedota com aparência de
    métrica — foi assim que uma composição de categorias marcou 5,3× numa
    janela e 1,1× na seguinte (#124).
    """
    grupo = grupo & base
    if len(grupo) < minimo:
        return None
    resto = base - grupo
    if not resto:
        return None
    ch = len(grupo & saiu)
    taxa = ch / len(grupo) * 100
    taxa_fora = len(resto & saiu) / len(resto) * 100
    return ResultadoSinal(
        nome=nome,
        n=len(grupo),
        churn_no_grupo=round(taxa, 1),
        churn_fora=round(taxa_fora, 1),
        lift=round(taxa / taxa_fora, 2) if taxa_fora else float("inf"),
        cobertura=round(ch / len(saiu) * 100, 1) if saiu else 0.0,
        cancelados_no_grupo=ch,
    )


@allow_cross_tenant(reason="backtest read-only; org passada explicitamente")
def rodar_backtest(
    organization: Organization,
    *,
    d0: date,
    horizonte: int = 120,
    amostra_minima: int = 15,
) -> Backtest:
    """Roda o backtest completo pra uma org. Não escreve nada."""
    from apps.customers.infrastructure.models import Contract
    from apps.financial.infrastructure.models import Invoice
    from apps.helpdesk.infrastructure.models import Ticket

    base_qs = Contract.objects.filter(
        organization=organization, activated_at__date__lte=d0
    ).filter(Q(canceled_at__isnull=True) | Q(canceled_at__date__gt=d0))
    base_por_id = dict(base_qs.values_list("id", "customer_external_id"))
    base = set(base_por_id)
    if not base:
        return Backtest(d0=d0, horizonte=horizonte, base=0, cancelados=0,
                        taxa_base=0.0, sinais=[])

    saiu = set(
        Contract.objects.filter(
            organization=organization,
            id__in=list(base),
            canceled_at__date__gt=d0,
            canceled_at__date__lte=d0 + timedelta(days=horizonte),
        ).values_list("id", flat=True)
    )

    cliente_para_contratos: dict[str, list[int]] = defaultdict(list)
    for cid, ext in base_por_id.items():
        cliente_para_contratos[ext].append(cid)

    sinais: list[ResultadoSinal] = []

    # ── Chamados frequentes ─────────────────────────────────────────────
    por_cliente: dict[str, int] = defaultdict(int)
    for ext in (
        Ticket.objects.filter(
            organization=organization,
            opened_at__date__gt=d0 - timedelta(days=TICKETS_WINDOW_DAYS),
            opened_at__date__lte=d0,
        )
        .values_list("customer_external_id", flat=True)
        .iterator()
    ):
        por_cliente[ext] += 1
    grupo = {
        cid
        for ext, n in por_cliente.items()
        if n >= TICKETS_MIN
        for cid in cliente_para_contratos.get(ext, ())
    }
    r = _avalia(
        f"FREQUENT_TICKETS (>={TICKETS_MIN} em {TICKETS_WINDOW_DAYS}d)",
        grupo, base, saiu, amostra_minima,
    )
    if r:
        sinais.append(r)

    # ── Atraso recorrente ───────────────────────────────────────────────
    # A fatura liga por CONTRATO; "em atraso em D0" = venceu até D0 e não tinha
    # pagamento até D0 (reconstruído da data, não do status atual).
    contrato_ext_para_id = dict(
        Contract.objects.filter(organization=organization, id__in=list(base))
        .values_list("external_id", "id")
    )
    atrasos: dict[str, int] = defaultdict(int)
    for cext, pago in (
        Invoice.objects.filter(
            organization=organization,
            due_date__gt=d0 - timedelta(days=LATE_PAYMENTS_WINDOW_DAYS),
            due_date__lte=d0,
        )
        .values_list("contract_external_id", "paid_at")
        .iterator(chunk_size=20000)
    ):
        pago_em = pago.date() if hasattr(pago, "date") else pago
        if pago_em is None or pago_em > d0:
            atrasos[cext] += 1
    grupo = {
        contrato_ext_para_id[cext]
        for cext, n in atrasos.items()
        if n >= LATE_PAYMENTS_MIN and cext in contrato_ext_para_id
    }
    r = _avalia(
        f"LATE_PAYMENTS (>={LATE_PAYMENTS_MIN} vencidas em "
        f"{LATE_PAYMENTS_WINDOW_DAYS}d)",
        grupo, base, saiu, amostra_minima,
    )
    if r:
        sinais.append(r)

    # ── O score que o modelo deu naquele dia (#123) ─────────────────────
    sinais.extend(
        _avalia_score_historico(organization, d0, base, saiu, amostra_minima)
    )

    return Backtest(
        d0=d0,
        horizonte=horizonte,
        base=len(base),
        cancelados=len(saiu),
        taxa_base=round(len(saiu) / len(base) * 100, 2),
        sinais=sinais,
    )


def _avalia_score_historico(
    organization: Organization,
    d0: date,
    base: set[int],
    saiu: set[int],
    amostra_minima: int,
) -> list[ResultadoSinal]:
    """Avalia o score realmente atribuído em D0, se houver histórico daquele dia.

    Esta é a validação sem reconstrução — a única que mede o algoritmo como ele
    rodou, e não como eu suponho que teria rodado. Só existe a partir da #123.
    """
    from apps.customers.infrastructure.models import Contract

    linhas = list(
        FactChurnRiskDaily.objects.filter(
            organization=organization, date=d0
        ).values_list("customer_id", "level")
    )
    if not linhas:
        return []

    contratos_do_cliente: dict[int, list[int]] = defaultdict(list)
    for cid, cust_id in Contract.objects.filter(
        organization=organization, id__in=list(base)
    ).values_list("id", "customer_id"):
        if cust_id is not None:
            contratos_do_cliente[cust_id].append(cid)

    por_nivel: dict[str, set[int]] = defaultdict(set)
    marcados: set[int] = set()
    for cust_id, nivel in linhas:
        ids = contratos_do_cliente.get(cust_id, ())
        por_nivel[nivel].update(ids)
        marcados.update(ids)

    resultados = []
    for nivel in ("HIGH", "MEDIUM", "LOW"):
        r = _avalia(
            f"score do dia: {nivel}", por_nivel[nivel], base, saiu, amostra_minima
        )
        if r:
            resultados.append(r)
    r = _avalia("score do dia: qualquer nível", marcados, base, saiu, amostra_minima)
    if r:
        resultados.append(r)
    return resultados


def formatar(bt: Backtest) -> str:
    """Relatório em texto — o que o comando imprime."""
    linhas = [
        f"D0 = {bt.d0} · horizonte = {bt.horizonte} dias",
        f"base em D0: {bt.base} contratos",
        f"cancelaram no horizonte: {bt.cancelados} ({bt.taxa_base}%)",
        "",
    ]
    if not bt.sinais:
        linhas.append("Nenhum sinal com amostra suficiente pra avaliar.")
        return "\n".join(linhas)

    linhas.append(
        f"{'sinal':44s} {'n':>6s} {'churn':>7s} {'fora':>7s} "
        f"{'lift':>6s} {'cobertura':>10s}"
    )
    linhas.append("-" * 84)
    for s in bt.sinais:
        marca = "" if s.separa else "  <- aponta pro lado errado"
        linhas.append(
            f"{s.nome:44s} {s.n:6d} {s.churn_no_grupo:6.1f}% {s.churn_fora:6.1f}% "
            f"{s.lift:5.2f}x {s.cobertura:9.1f}%{marca}"
        )
    return "\n".join(linhas)
