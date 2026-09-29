"""Auditoria semântica de churn — o que a aba "Auditoria de Churn" mostra.

Busca os contratos cancelados, passa cada um pelo classificador do domínio
(`apps.analytics.domain.churn_audit`) e devolve o que a página mostra: KPIs por
nível de controle, a fila de prioridade, os Paretos e a tabela auditável.

O recorte é pela **competência real**, não pela data de cancelamento: um
inadimplente bloqueado em junho e baixado em setembro pesa em julho. Por isso a
busca no banco começa 31 dias antes da janela e não tem fim — o cancelamento
feito amanhã pode cair num mês que já está na tela. Consequência que a página
precisa dizer: os meses recentes ainda recebem cancelamentos retroativos.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models.fields.json import KeyTextTransform

from apps.analytics.domain import churn_audit as dom
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

from .aggregations import motivo_label

# Re-exportados para a camada de apresentação validar a querystring.
CAUSAS_CONHECIDAS = dom.CAUSAS_CONHECIDAS
NIVEL_SLUGS = dom.NIVEL_SLUGS

_TZ = ZoneInfo("America/Sao_Paulo")
_MESES = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")

# Motivos cadastrados com cor própria no gráfico mensal, na ordem dos slots da
# paleta categórica (charts.py). O resto vira "Outros". Cor fixa por motivo, não
# por ranking: filtrar um mês não pode repintar os outros.
MOTIVOS_EM_DESTAQUE: tuple[str, ...] = (
    "Inadimplência",
    "Mudou de operadora",
    "Desconexão por opção",
    "Mudou de endereço",
    "Endereço não cabeado",
    "Mudou de cidade",
    "Problemas técnicos",
    "Troca de titularidade",
)
OUTROS = "Outros"


def mes_label(d: date) -> str:
    """"mar/26" — o rótulo de competência que a auditoria original usava."""
    return f"{_MESES[d.month - 1]}/{d.year % 100:02d}"


def _data_ixc(bruto: Any) -> date | None:
    """Data do raw_extras do IXC ("2026-04-14" ou com hora); vazio/zerado → None."""
    texto = str(bruto or "").strip()[:10]
    if not texto or texto.startswith("0000"):
        return None
    try:
        return date.fromisoformat(texto)
    except ValueError:
        return None


@dataclass(frozen=True)
class RegistroAuditado:
    """Um cancelamento com o veredito e a competência já calculados."""

    contrato_id: int
    contrato_external_id: str
    cliente_id: int | None
    cliente: str
    plano: str
    mrr: Decimal
    cancelado_em: date
    motivo: str
    observacao: str
    veredito: dom.Veredito
    competencia: dom.Competencia

    @property
    def competencia_mes(self) -> str:
        return mes_label(self.competencia.data)

    @property
    def competencia_chave(self) -> str:
        return self.competencia.data.strftime("%Y-%m")

    @property
    def nivel(self) -> dom.Nivel:
        return self.veredito.nivel

    @property
    def motivo_grafico(self) -> str:
        return self.motivo if self.motivo in MOTIVOS_EM_DESTAQUE else OUTROS


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def auditar_cancelamentos(
    organization: Organization, *, inicio: date, fim: date
) -> list[RegistroAuditado]:
    """Cancelamentos cuja competência real cai em [inicio, fim], auditados.

    Só contratos que chegaram a ser ativados: pré-contrato desistido nunca foi
    receita, e já fica fora do churn da página de Churn pelo mesmo motivo.
    """
    from apps.customers.infrastructure.models import Contract

    # A competência de inadimplência é o bloqueio + 30 dias, e o bloqueio vem
    # antes do cancelamento: nada que tenha sido cancelado mais de 31 dias
    # antes do início da janela pode cair dentro dela.
    busca_desde = inicio - timedelta(days=dom.DIAS_APOS_BLOQUEIO + 1)
    linhas = (
        Contract.objects.filter(
            organization=organization,
            status=Contract.Status.CANCELED,
            activated_at__isnull=False,
            canceled_at__isnull=False,
            canceled_at__date__gte=busca_desde,
        )
        .values(
            "id",
            "external_id",
            "customer_id",
            "customer__name",
            "plan_name",
            "monthly_amount",
            "canceled_at",
            motivo_id=KeyTextTransform("motivo_cancelamento", "raw_extras"),
            obs=KeyTextTransform("obs_cancelamento", "raw_extras"),
            bloqueio_auto=KeyTextTransform("dt_ult_bloq_auto", "raw_extras"),
            bloqueio_manual=KeyTextTransform("dt_ult_bloq_manual", "raw_extras"),
        )
        .order_by("-canceled_at", "-id")
    )

    registros: list[RegistroAuditado] = []
    for linha in linhas.iterator():
        cancelado_em: datetime = linha["canceled_at"]
        motivo = motivo_label(str(linha["motivo_id"] or "0"))
        observacao = str(linha["obs"] or "").strip()
        veredito = dom.classificar(motivo, observacao)
        competencia = dom.competencia(
            veredito.causa_raiz,
            cancelado_em.astimezone(_TZ).date(),
            bloqueio_auto=_data_ixc(linha["bloqueio_auto"]),
            bloqueio_manual=_data_ixc(linha["bloqueio_manual"]),
        )
        if not inicio <= competencia.data <= fim:
            continue
        registros.append(
            RegistroAuditado(
                contrato_id=linha["id"],
                contrato_external_id=linha["external_id"],
                cliente_id=linha["customer_id"],
                cliente=linha["customer__name"] or "Nome não informado",
                plano=linha["plan_name"] or "—",
                mrr=linha["monthly_amount"] or Decimal("0"),
                cancelado_em=cancelado_em.astimezone(_TZ).date(),
                motivo=motivo,
                observacao=observacao,
                veredito=veredito,
                competencia=competencia,
            )
        )
    registros.sort(
        key=lambda r: (r.competencia.data, r.cancelado_em, r.contrato_id), reverse=True
    )
    return registros


# =============================================================================
# Filtros — os mesmos quatro da auditoria original
# =============================================================================


@dataclass(frozen=True)
class Filtros:
    competencia: str | None = None  # "YYYY-MM"
    nivel: str | None = None  # slug de NivelInfo
    causa: str | None = None
    busca: str | None = None

    @property
    def algum(self) -> bool:
        return any((self.competencia, self.nivel, self.causa, self.busca))


_NIVEL_POR_SLUG: dict[str, dom.Nivel] = {info.slug: info.nivel for info in dom.NIVEIS}


def filtrar(registros: Iterable[RegistroAuditado], filtros: Filtros) -> list[RegistroAuditado]:
    nivel = _NIVEL_POR_SLUG.get(filtros.nivel or "")
    termo = dom.normalizar(filtros.busca or "")
    saida = []
    for r in registros:
        if filtros.competencia and r.competencia_chave != filtros.competencia:
            continue
        if nivel is not None and r.nivel is not nivel:
            continue
        if filtros.causa and r.veredito.causa_raiz != filtros.causa:
            continue
        if termo:
            alvo = dom.normalizar(
                " ".join(
                    (r.contrato_external_id, r.cliente, r.motivo, r.observacao, r.veredito.causa_raiz)
                )
            )
            if termo not in alvo:
                continue
        saida.append(r)
    return saida


# =============================================================================
# O que a página mostra
# =============================================================================


def _contagem(n: int, mrr: Decimal) -> dict[str, Any]:
    return {"n": n, "mrr": float(mrr)}


def _soma(registros: Iterable[RegistroAuditado]) -> dict[str, Any]:
    n = 0
    mrr = Decimal("0")
    for r in registros:
        n += 1
        mrr += r.mrr
    return _contagem(n, mrr)


def _barras(contagem: Counter[str], *, sem: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Barras horizontais ordenadas, com a largura relativa ao maior valor."""
    itens = [(k, v) for k, v in contagem.most_common() if k and k not in sem]
    maior = max((v for _, v in itens), default=0)
    return [
        {"label": k, "n": v, "pct_barra": round(v / maior * 100, 1) if maior else 0.0}
        for k, v in itens
    ]


def _meses_da_janela(inicio: date, fim: date) -> list[date]:
    meses = []
    atual = inicio.replace(day=1)
    while atual <= fim:
        meses.append(atual)
        atual = (atual + timedelta(days=32)).replace(day=1)
    return meses


def _series_mensais(
    registros: list[RegistroAuditado], meses: list[date]
) -> dict[str, Any]:
    """Volume por mês de competência, empilhado por motivo e por nível."""
    chaves = [m.strftime("%Y-%m") for m in meses]
    por_motivo: dict[str, Counter[str]] = {m: Counter() for m in (*MOTIVOS_EM_DESTAQUE, OUTROS)}
    por_nivel: dict[dom.Nivel, Counter[str]] = {info.nivel: Counter() for info in dom.NIVEIS}
    totais: Counter[str] = Counter()
    for r in registros:
        por_motivo[r.motivo_grafico][r.competencia_chave] += 1
        por_nivel[r.nivel][r.competencia_chave] += 1
        totais[r.competencia_chave] += 1
    return {
        "labels": [mes_label(m) for m in meses],
        "chaves": chaves,
        "totais": [totais[k] for k in chaves],
        "por_motivo": [
            {"nome": motivo, "valores": [por_motivo[motivo][k] for k in chaves]}
            for motivo in (*MOTIVOS_EM_DESTAQUE, OUTROS)
            if any(por_motivo[motivo].values())
        ],
        # "Fora do churn" não entra: titularidade e saneamento não são perda.
        "por_nivel": [
            {
                "nome": info.nivel.value,
                "slug": info.slug,
                "valores": [por_nivel[info.nivel][k] for k in chaves],
            }
            for info in dom.NIVEIS
            if info.nivel is not dom.Nivel.FORA
        ],
        "churn_real": [
            totais[k] - por_nivel[dom.Nivel.FORA][k] for k in chaves
        ],
    }


def _mudanca_rotulo(causa: str) -> str:
    """No card de mudança, o prefixo "Mudança — " é redundante."""
    if causa == dom.MUD_SEM_COBERTURA:
        return "Local explicitamente não atendido"
    rotulo = causa.replace("Mudança — ", "")
    return rotulo[:1].upper() + rotulo[1:]


@allow_cross_tenant(reason="aggregations rodam fora de request HTTP")
def compute_churn_audit(
    organization: Organization,
    *,
    inicio: date,
    fim: date,
    filtros: Filtros | None = None,
) -> dict[str, Any]:
    """Tudo o que a página de Auditoria de Churn mostra, já recortado."""
    filtros = filtros or Filtros()
    todos = auditar_cancelamentos(organization, inicio=inicio, fim=fim)
    # Trocar o período mantém a querystring: uma competência que ficou fora da
    # nova janela esvaziaria a página em silêncio. Ela simplesmente cai.
    if filtros.competencia and filtros.competencia not in {r.competencia_chave for r in todos}:
        filtros = replace(filtros, competencia=None)
    registros = filtrar(todos, filtros)

    por_nivel: dict[dom.Nivel, list[RegistroAuditado]] = {info.nivel: [] for info in dom.NIVEIS}
    for r in registros:
        por_nivel[r.nivel].append(r)

    fora = por_nivel[dom.Nivel.FORA]
    kpis = {
        "auditados": _soma(registros),
        "churn_real": _soma(r for r in registros if r.nivel is not dom.Nivel.FORA),
        "retencao_direta": _soma(r for r in registros if r.nivel in dom.RETENCAO_DIRETA),
        "parcial": _soma(por_nivel[dom.Nivel.PARCIAL]),
        "inevitavel": _soma(por_nivel[dom.Nivel.INEVITAVEL]),
        "fora": _soma(fora),
        "inconsistentes": sum(1 for r in registros if r.veredito.inconsistente),
    }
    maior_nivel = max((len(v) for v in por_nivel.values()), default=0)
    niveis = [
        {
            "nivel": info.nivel.value,
            "prioridade": info.prioridade,
            "slug": info.slug,
            **_soma(por_nivel[info.nivel]),
            "pct_barra": round(len(por_nivel[info.nivel]) / maior_nivel * 100, 1)
            if maior_nivel
            else 0.0,
        }
        for info in dom.NIVEIS
    ]

    tipo = dom.motivo_tipo
    operadora = [r for r in registros if tipo(r.motivo) is dom.MotivoTipo.OPERADORA]
    tecnicos = [r for r in registros if tipo(r.motivo) is dom.MotivoTipo.TECNICO]
    enderecos = [r for r in registros if tipo(r.motivo) is dom.MotivoTipo.ENDERECO]

    return {
        "registros": registros,
        "filtros": filtros,
        "total_na_janela": len(todos),
        "kpis": kpis,
        "niveis": niveis,
        "mensal": _series_mensais(registros, _meses_da_janela(inicio, fim)),
        "pareto": _barras(
            Counter(r.veredito.causa_raiz for r in registros if r.nivel in dom.RETENCAO_DIRETA)
        ),
        "concorrencia": {
            "cadastrados": len(operadora),
            "barras": _barras(
                Counter(r.veredito.subtipo_concorrencia for r in operadora),
                sem=frozenset({dom.NAO_SE_APLICA}),
            ),
        },
        "tecnicos": {
            "cadastrados": len(tecnicos),
            "barras": _barras(
                Counter(r.veredito.subtipo_tecnico for r in tecnicos),
                sem=frozenset({dom.NAO_SE_APLICA}),
            ),
        },
        "mudancas": {
            "cadastrados": len(enderecos),
            "barras": _barras(Counter(_mudanca_rotulo(r.veredito.causa_raiz) for r in enderecos)),
        },
        # Opções dos filtros saem da janela inteira, não do recorte: escolher
        # um nível não pode sumir com as outras competências do seletor.
        "opcoes": {
            "competencias": sorted(
                {(r.competencia_chave, r.competencia_mes) for r in todos}, reverse=True
            ),
            "causas": sorted({r.veredito.causa_raiz for r in todos}),
        },
    }
