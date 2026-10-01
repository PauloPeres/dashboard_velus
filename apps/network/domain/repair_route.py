"""Por onde o técnico começa: a primeira caixa afetada, e onde deve estar o rompimento.

Segunda metade do pedido do técnico (`docs/massivas-rota-do-tecnico-plano.md`).
O `plant_graph` responde *quem vem antes de quem*; aqui se responde **onde
descer do poste**.

## O raciocínio

Cada elemento da planta, com tudo o que pende dele, é uma **hipótese**: "o
rompimento é logo acima daqui". A hipótese ganha nota por quanto ela explica o
que se vê — quantas caixas fora ficam abaixo dela contra quantas caixas **com
cliente online** também ficam abaixo dela e, pela hipótese, deveriam ter caído.
Vence a de maior nota; o trecho rompido é o pedaço de cabo entre ela e o
elemento de cima, e é esse pedaço que o "X" marca.

A nota é a verossimilhança de um modelo de dois estados: abaixo do corte cada
caixa cai com uma probabilidade, fora dele com outra, e as duas saem dos
próprios números da massiva. Não há limiar para ajustar — e isso importa,
porque um limiar escolhido olhando para uma massiva só é sob medida para ela.

## Por que não "o primeiro elemento com todos os clientes fora"

Era a regra até 30/09/2026, e a massiva 359 (72FO rompido, 490 clientes) a
derrubou duas vezes:

- **uma caixa fora de lugar anula tudo.** Das 220 caixas fora, 13 não pendiam do
  72FO — quase todas caixas mistas com um cliente só caído. Exigindo 100%, o
  ponto comum subiu até o POP e o "comece por aqui" foi parar a 1.155 m do
  rompimento;
- **caixa vazia contava como "no ar".** Abaixo do trecho rompido havia 114
  caixas "no ar", e 109 delas não tinham nenhum cliente ativo. Com isso "todos
  abaixo caíram" nunca se confirma. Quem não tem cliente não é evidência de
  nada — por isso `ctos_conhecidas` agora é quem tem cliente online.

No backtest da 359, esta regra apontou o trecho certo (a 13 m do rompimento), e
continuou acertando vendo só 5 das 220 caixas fora ou com 60 caixas de ruído
somadas (`docs/massivas-backtest-359.md`).

## O que continua valendo

**Nada disto é leitura da fibra.** É topologia inferida de cadastro: o IXC não
expõe a fusão, e duas caixas que o desenho põe no mesmo cabo podem estar em
fibras diferentes dele. Por isso cada resposta daqui carrega a cobertura e as
contas da nota, e a tela que usar isto diz "provável" — a palavra não é
gentileza, é a diferença entre o técnico conferir e o técnico cavar.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace

from .plant_graph import CEO, CTO, POP, Edge, Graph, Hop, NodeInput, NodeKey, route_to

# Nome da regra que produziu a resposta. Vai junto da aferição (o erro medido
# contra o rompimento registrado): sem ele, trocar a regra misturaria na mesma
# série erros de algoritmos diferentes.
ALGORITMO = "verossimilhanca-v1"


@dataclass(frozen=True)
class CaixaStatus:
    """Uma caixa da rota e o que pende dela: quem caiu, quem não."""

    node: NodeInput
    metros_do_pop: float
    ctos_fora: tuple[NodeInput, ...]
    ctos_no_ar: tuple[NodeInput, ...]

    @property
    def tudo_fora(self) -> bool:
        """Todos os clientes abaixo dela caíram — o problema está nela ou acima."""
        return bool(self.ctos_fora) and not self.ctos_no_ar

    @property
    def total(self) -> int:
        return len(self.ctos_fora) + len(self.ctos_no_ar)


# Cobertura mínima para o sistema se atrever a dizer ONDE está o rompimento.
#
# A inferência depende de saber quem **não** caiu; se metade das CTOs afetadas
# não está no grafo, a lista de "ainda no ar" abaixo de uma caixa pode conter
# gente que caiu e não foi mapeada — e aí o X aponta para o lugar errado.
# Medido em produção: massivas com 1 de 16 CTAs no grafo existiam, e para elas
# o sistema apontava um trecho com a mesma confiança de uma com 6 de 7.
COBERTURA_MINIMA = 0.5
CTOS_MINIMAS_NO_GRAFO = 2

# Quando a melhor hipótese é boa o bastante para virar "comece por aqui".
#
# A nota ordena; estes dois números decidem se a primeira colocada merece ser
# afirmada. Explicar 80% das caixas fora deixa margem para a caixa mista e para
# a caixa mal desenhada (na 359 foram 94%); 80% de pureza deixa margem para o
# cliente que voltou antes da hora (na 359, 99%). Abaixo disso, a tela mostra as
# hipóteses com as contas e não crava nenhuma — pode ser mais de um evento.
#
# São ponto de partida, não medida: é para calibrá-los que a massiva guarda o
# local do rompimento registrado e o erro de cada resposta.
EXPLICA_MINIMA = 0.8
PUREZA_MINIMA = 0.8

# Quantas hipóteses a tela mostra. A primeira é a resposta; as outras duas são
# para onde o técnico olha se a primeira não for.
HIPOTESES_NA_TELA = 3


@dataclass(frozen=True)
class HipoteseDeRompimento:
    """Hipótese: "o rompimento é logo acima deste elemento" — com a nota e as contas.

    `acima` e `aresta` são vazios quando o elemento é a própria origem da rota
    (o POP): aí a hipótese é "o problema está no POP ou na saída dele", e não há
    pedaço de cabo para marcar.
    """

    no: NodeInput
    acima: NodeInput | None
    aresta: Edge | None
    fora_abaixo: int
    no_ar_abaixo: int
    fora_total: int
    verossimilhanca: float
    metros_do_pop: float
    # Os pedaços de cabo que nenhum dado separa um do outro, de `acima` até o
    # primeiro elemento com evidência própria — ver `_estender_pela_cadeia`.
    # Vazio até ser estendida; com um passo só quando `no` já tem evidência.
    cadeia: tuple[tuple[NodeInput, NodeInput, Edge], ...] = ()

    @property
    def fim(self) -> NodeInput:
        """Onde o trecho termina: o primeiro elemento com cliente abaixo da cadeia."""
        return self.cadeia[-1][1] if self.cadeia else self.no

    @property
    def metros(self) -> float:
        """Comprimento do trecho inteiro — a cadeia toda, não só o primeiro pedaço."""
        if self.cadeia:
            return sum(aresta.metros for _, _, aresta in self.cadeia)
        return self.aresta.metros if self.aresta else 0.0

    @property
    def explica(self) -> float:
        """Que parte das caixas fora fica abaixo deste ponto."""
        return self.fora_abaixo / self.fora_total if self.fora_total else 0.0

    @property
    def pureza(self) -> float:
        """Das caixas com cliente abaixo deste ponto, que parte caiu."""
        total = self.fora_abaixo + self.no_ar_abaixo
        return self.fora_abaixo / total if total else 0.0

    @property
    def forte(self) -> bool:
        return self.explica >= EXPLICA_MINIMA and self.pureza >= PUREZA_MINIMA

    @property
    def cabo_id(self) -> str:
        return self.aresta.cabo_id if self.aresta else ""


@dataclass
class RotaDoTecnico:
    """O que a tela e a mensagem precisam dizer."""

    partida: NodeInput | None = None
    partida_status: CaixaStatus | None = None
    # Quando não há POP na rota, "primeira" não pode ser afirmado: o que se sabe
    # é que a caixa é comum a todas as CTOs afetadas. A tela usa palavras
    # diferentes para as duas coisas.
    tem_origem_no_pop: bool = False
    primeira_cto: NodeInput | None = None
    caixa_anterior: NodeInput | None = None
    trecho_rompido: tuple[NodeInput, NodeInput] | None = None
    cabo_rompido: Edge | None = None
    # O trecho pedaço a pedaço, de `caixa_anterior` até a `partida`. Mais de um
    # pedaço quando há junção, emenda ou caixa vazia no meio do caminho.
    trecho_passos: list[tuple[NodeInput, NodeInput, Edge]] = field(default_factory=list)
    caixas: list[CaixaStatus] = field(default_factory=list)
    rota: list[Hop] = field(default_factory=list)
    # As melhores hipóteses, uma por cabo. A primeira é a que virou partida
    # quando é forte; quando não é, elas vão para a tela como hipóteses, com as
    # contas, e nenhuma é afirmada.
    hipoteses: list[HipoteseDeRompimento] = field(default_factory=list)
    # Cobertura: o mapa é menor que a massiva, e a tela declara isso.
    ctos_afetadas: int = 0
    ctos_no_grafo: int = 0

    # A partida foi confirmada pela nota? Quando não, o que se tem é o ponto
    # comum das rotas — verdade menor, dita como tal.
    partida_confirmada: bool = False

    @property
    def completa(self) -> bool:
        return self.partida is not None

    @property
    def cobertura_suficiente(self) -> bool:
        """Dá para afirmar onde é o rompimento com o que está no grafo?"""
        if self.ctos_no_grafo < CTOS_MINIMAS_NO_GRAFO:
            return False
        if not self.ctos_afetadas:
            return False
        return self.ctos_no_grafo / self.ctos_afetadas >= COBERTURA_MINIMA


def _plogp(a: int, b: int) -> float:
    """Σ x·log(x/(a+b)) — a log-verossimilhança de uma moeda com a e b."""
    total = a + b
    return sum(x * math.log(x / total) for x in (a, b) if x > 0)


def _verossimilhanca(fora_abaixo: int, no_ar_abaixo: int, fora_fora: int, no_ar_fora: int) -> float:
    """Nota de um corte: quão bem "abaixo daqui caiu, fora daqui não" explica tudo.

    Cada lado do corte é uma moeda com a sua própria taxa de queda, estimada dos
    próprios números. Um corte que separa bem (abaixo quase tudo caiu, fora
    quase nada) soma perto de zero; um que mistura soma muito negativo.
    """
    return _plogp(fora_abaixo, no_ar_abaixo) + _plogp(fora_fora, no_ar_fora)


def _filhos(anterior: dict[NodeKey, tuple[NodeKey, Edge]]) -> dict[NodeKey, list[NodeKey]]:
    filhos: dict[NodeKey, list[NodeKey]] = {}
    for no, (pai, _) in anterior.items():
        filhos.setdefault(pai, []).append(no)
    return filhos


def _abaixo(filhos: dict[NodeKey, list[NodeKey]], raiz: NodeKey) -> set[NodeKey]:
    """A raiz e tudo que pende dela na árvore de distâncias.

    É a árvore do Dijkstra e não "tudo que fica mais longe do POP": cada
    elemento pende de um pai só. Com a regra antiga (qualquer vizinho mais
    distante), um elemento contava abaixo de duas caixas ao mesmo tempo, e a
    nota de uma hipótese contaria a mesma caixa fora duas vezes.
    """
    saida = {raiz}
    pilha = [raiz]
    while pilha:
        for filho in filhos.get(pilha.pop(), ()):
            saida.add(filho)
            pilha.append(filho)
    return saida


@dataclass(frozen=True)
class _Contagem:
    """Quantas caixas fora e no ar pendem de cada elemento da árvore."""

    filhos: dict[NodeKey, list[NodeKey]]
    fora_abaixo: dict[NodeKey, int]
    no_ar_abaixo: dict[NodeKey, int]
    fora: frozenset[NodeKey]
    no_ar: frozenset[NodeKey]


def _contar(
    dist: dict[NodeKey, float],
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    fora: Collection[NodeKey],
    no_ar: Collection[NodeKey],
) -> _Contagem:
    fora_set = frozenset(k for k in fora if k in dist)
    no_ar_set = frozenset(k for k in no_ar if k in dist) - fora_set
    # De baixo para cima: cada nó soma a si e aos filhos. Ordenar por distância
    # decrescente garante filho antes do pai — a aresta tem piso de 1 m, então o
    # filho está sempre estritamente mais longe.
    filhos = _filhos(anterior)
    fora_abaixo: dict[NodeKey, int] = {}
    no_ar_abaixo: dict[NodeKey, int] = {}
    for chave in sorted(dist, key=lambda k: -dist[k]):
        f = 1 if chave in fora_set else 0
        u = 1 if chave in no_ar_set else 0
        for filho in filhos.get(chave, ()):
            f += fora_abaixo[filho]
            u += no_ar_abaixo[filho]
        fora_abaixo[chave] = f
        no_ar_abaixo[chave] = u
    return _Contagem(filhos, fora_abaixo, no_ar_abaixo, fora_set, no_ar_set)


def _ranquear(
    grafo: Graph,
    dist: dict[NodeKey, float],
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    contagem: _Contagem,
) -> list[HipoteseDeRompimento]:
    total_fora, total_no_ar = len(contagem.fora), len(contagem.no_ar)
    if not total_fora:
        return []
    hipoteses: list[HipoteseDeRompimento] = []
    for chave, f in contagem.fora_abaixo.items():
        if not f or chave not in grafo.nodes:
            continue
        u = contagem.no_ar_abaixo[chave]
        pai = anterior.get(chave)
        hipoteses.append(
            HipoteseDeRompimento(
                no=grafo.nodes[chave],
                acima=grafo.nodes.get(pai[0]) if pai else None,
                aresta=pai[1] if pai else None,
                fora_abaixo=f,
                no_ar_abaixo=u,
                fora_total=total_fora,
                verossimilhanca=_verossimilhanca(f, u, total_fora - f, total_no_ar - u),
                metros_do_pop=dist[chave],
            )
        )
    hipoteses.sort(
        key=lambda h: (-h.verossimilhanca, h.metros_do_pop, h.no.label, h.no.external_id)
    )
    return hipoteses


def ranquear_hipoteses(
    grafo: Graph,
    dist: dict[NodeKey, float],
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    *,
    fora: Collection[NodeKey],
    no_ar: Collection[NodeKey],
) -> list[HipoteseDeRompimento]:
    """Todos os elementos que explicam ao menos uma caixa fora, da maior nota à menor.

    Empate vai para o mais perto do POP: numa sequência de elementos sem nada
    pendurado no meio, a nota é a mesma do começo ao fim, e o técnico começa
    por cima — logo abaixo da última caixa que continua no ar.
    """
    return _ranquear(grafo, dist, anterior, _contar(dist, anterior, fora, no_ar))


def _estender_pela_cadeia(
    h: HipoteseDeRompimento,
    grafo: Graph,
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    contagem: _Contagem,
) -> HipoteseDeRompimento:
    """Desce pela sequência de pedaços que nenhum dado separa um do outro.

    Entre a última caixa com cliente no ar e a primeira com cliente fora pode
    haver junção, emenda, caixa vazia — elementos sem evidência própria. Todos
    os pedaços entre eles têm a mesma nota: o dado não diz em qual deles rompeu.
    Apontar só o primeiro (o mais perto do POP) era o que a regra fazia — e no
    teste cego de 01/10/2026, com rompimentos sorteados na planta inteira, em
    31% dos cortes limpos o rompimento estava num pedaço de baixo da mesma
    sequência. O trecho honesto é a sequência inteira: é como o técnico anda,
    da última caixa boa até a primeira caixa caída.

    Para onde a evidência se divide (dois ramos com cliente) ou onde o próprio
    elemento tem cliente: ali o dado volta a falar, e a cadeia termina.
    """
    if h.acima is None or h.aresta is None:
        return h
    passos: list[tuple[NodeInput, NodeInput, Edge]] = [(h.acima, h.no, h.aresta)]
    atual = h.no.key
    alvo = (contagem.fora_abaixo[atual], contagem.no_ar_abaixo[atual])
    evidencia = contagem.fora | contagem.no_ar
    while atual not in evidencia:
        seguintes = [
            f for f in contagem.filhos.get(atual, ())
            if (contagem.fora_abaixo.get(f, 0), contagem.no_ar_abaixo.get(f, 0)) == alvo
        ]
        if len(seguintes) != 1 or seguintes[0] not in grafo.nodes:
            break
        proximo = seguintes[0]
        passos.append((grafo.nodes[atual], grafo.nodes[proximo], anterior[proximo][1]))
        atual = proximo
    return replace(h, cadeia=tuple(passos))


def _escolher_hipoteses(
    ranking: Sequence[HipoteseDeRompimento],
    grafo: Graph,
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    contagem: _Contagem,
    limite: int = HIPOTESES_NA_TELA,
) -> list[HipoteseDeRompimento]:
    """As melhores, já com a cadeia — uma por cabo, e nenhuma dentro da cadeia de outra.

    Os pedaços de uma mesma cadeia empatam na nota e vinham como 1ª, 2ª e 3ª
    hipóteses: a mesma resposta dita três vezes. Com a cadeia, eles viram uma
    hipótese só, e as outras duas passam a ser lugares diferentes de verdade.
    """
    vistas: set[str] = set()
    dentro: set[NodeKey] = set()
    saida: list[HipoteseDeRompimento] = []
    for h in ranking:
        if h.no.key in dentro:
            continue
        chave = h.cabo_id or f"origem:{h.no.kind}:{h.no.external_id}"
        if chave in vistas:
            continue
        estendida = _estender_pela_cadeia(h, grafo, anterior, contagem)
        vistas.add(chave)
        dentro.update(no.key for _, no, _ in estendida.cadeia)
        dentro.add(h.no.key)
        saida.append(estendida)
        if len(saida) == limite:
            break
    return saida


def hipoteses_distintas(
    hipoteses: Sequence[HipoteseDeRompimento], limite: int = HIPOTESES_NA_TELA
) -> list[HipoteseDeRompimento]:
    """As melhores, uma por cabo.

    Sem isto, as três primeiras seriam quase sempre três pedaços seguidos do
    mesmo cabo — a mesma resposta dita três vezes. Uma por cabo é o que diz ao
    técnico para onde olhar se a primeira não for: o cabo de baixo, o de cima.
    """
    vistas: set[str] = set()
    saida: list[HipoteseDeRompimento] = []
    for h in hipoteses:
        chave = h.cabo_id or f"origem:{h.no.kind}:{h.no.external_id}"
        if chave in vistas:
            continue
        vistas.add(chave)
        saida.append(h)
        if len(saida) == limite:
            break
    return saida


def _prefixo_comum(rotas: Sequence[list[Hop]]) -> list[Hop]:
    """O pedaço de rota que todas as rotas compartilham."""
    if not rotas:
        return []
    comum: list[Hop] = []
    for passos in zip(*rotas, strict=False):
        primeiro = passos[0]
        if all(p.node.key == primeiro.node.key for p in passos):
            comum.append(primeiro)
        else:
            break
    return comum


def _status(
    grafo: Graph,
    filhos: dict[NodeKey, list[NodeKey]],
    node: NodeInput,
    metros_do_pop: float,
    *,
    fora: set[NodeKey],
    no_ar: set[NodeKey],
) -> CaixaStatus:
    ctos = {k for k in _abaixo(filhos, node.key) if k[0] == CTO}
    return CaixaStatus(
        node,
        metros_do_pop,
        tuple(grafo.nodes[k] for k in sorted(ctos & fora) if k in grafo.nodes),
        tuple(grafo.nodes[k] for k in sorted(ctos & no_ar) if k in grafo.nodes),
    )


def calcular(
    grafo: Graph,
    dist: dict[NodeKey, float],
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    *,
    ctos_afetadas: Sequence[NodeKey],
    ctos_conhecidas: Sequence[NodeKey],
) -> RotaDoTecnico:
    """Monta a rota do técnico a partir das CTOs que caíram.

    `ctos_conhecidas` é quem serve de evidência: as afetadas e as que têm
    cliente online. Quem sobra (conhecida e não afetada) é o "no ar" — e caixa
    sem cliente não entra, porque não é prova de nada.
    """
    afetadas = {c for c in ctos_afetadas if c in dist}
    saida = RotaDoTecnico(
        ctos_afetadas=len(set(ctos_afetadas)),
        ctos_no_grafo=len(afetadas),
    )
    if not afetadas:
        return saida

    no_ar = {c for c in ctos_conhecidas if c in dist} - afetadas
    contagem = _contar(dist, anterior, afetadas, no_ar)
    ranking = _ranquear(grafo, dist, anterior, contagem)
    saida.hipoteses = _escolher_hipoteses(ranking, grafo, anterior, contagem)
    filhos = contagem.filhos

    melhor = saida.hipoteses[0] if saida.hipoteses and saida.hipoteses[0].forte else None
    if melhor is not None:
        # A rota desce até onde as caixas que a hipótese explica ainda andam
        # juntas: passa pela partida e mostra as emendas logo abaixo dela.
        explicadas = afetadas & _abaixo(filhos, melhor.no.key)
        base = explicadas
    else:
        # Nenhuma hipótese explica sozinha a massiva: o que se pode afirmar é o
        # ponto mais fundo compartilhado, sem chamá-lo de causa. Aconteceu em
        # produção com 2 CTOs fora e 35 no ar abaixo da mesma caixa — dizer
        # "rompimento aqui" ali seria mandar o técnico subir num poste bom.
        base = afetadas

    rotas = [route_to(grafo, dist, anterior, c) for c in sorted(base)]
    comum = _prefixo_comum([r for r in rotas if r])
    if not comum:
        return saida

    saida.rota = comum
    saida.tem_origem_no_pop = comum[0].node.kind == POP
    saida.caixas = [
        _status(grafo, filhos, hop.node, hop.metros_do_pop, fora=afetadas, no_ar=no_ar)
        for hop in comum
    ]

    if melhor is not None:
        # A partida é o fim da cadeia: o primeiro elemento com cliente caído
        # abaixo da última caixa boa. Sem cadeia (o elemento já tem cliente),
        # é o próprio elemento da hipótese.
        saida.partida = melhor.fim
        saida.partida_confirmada = True
        # O trecho (e o X que sai dele) só existe quando há elemento acima — na
        # origem não existe "entre" — e o grafo tem massiva bastante para
        # sustentar a conclusão.
        if melhor.acima is not None and saida.cobertura_suficiente:
            saida.caixa_anterior = melhor.acima
            saida.trecho_rompido = (melhor.acima, melhor.fim)
            saida.trecho_passos = list(melhor.cadeia)
            # O cabo do primeiro pedaço: é o nome que o técnico procura no
            # poste ao sair da última caixa boa.
            saida.cabo_rompido = melhor.aresta
    else:
        saida.partida = comum[-1].node
    na_rota = [c for c in saida.caixas if c.node.key == saida.partida.key]
    saida.partida_status = (
        na_rota[0]
        if na_rota
        else _status(
            grafo,
            filhos,
            saida.partida,
            dist.get(saida.partida.key, 0.0),
            fora=afetadas,
            no_ar=no_ar,
        )
    )

    # A primeira CTO afetada é a que está mais perto do POP entre as que caíram.
    candidatas = [(dist[c], c) for c in afetadas]
    if candidatas:
        saida.primeira_cto = grafo.nodes[min(candidatas)[1]]

    return saida


def ceos_da_rota(rota: RotaDoTecnico) -> list[CaixaStatus]:
    """Só as caixas de emenda, que é o que o técnico baixa do poste."""
    return [c for c in rota.caixas if c.node.kind == CEO]
