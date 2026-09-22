"""Por onde o técnico começa: a primeira caixa afetada, e onde deve estar o rompimento.

Segunda metade do pedido do técnico (`docs/massivas-rota-do-tecnico-plano.md`).
O `plant_graph` responde *quem vem antes de quem*; aqui se responde **onde
descer do poste**.

O raciocínio é o mesmo que ele faz na cabeça, escrito:

1. as CTOs que caíram estão todas abaixo de algum ponto comum da rota — se não
   estivessem, seriam duas massivas, não uma;
2. descendo do POP, o primeiro elemento cujos clientes **todos** caíram é o
   primeiro elemento afetado. Acima dele ainda há gente no ar;
3. logo, o rompimento está **entre esse elemento e o anterior**, que continua no
   ar. É esse pedaço de cabo que o "X" marca;
4. a caixa anterior entra no desenho junto (pedido 9): sem ela o "X" flutua, e
   com ela o técnico vê de onde a fibra vem.

**Nada disto é leitura da fibra.** É topologia inferida de cadastro: o IXC não
expõe a fusão, e duas caixas que o desenho põe no mesmo cabo podem estar em
fibras diferentes dele. Por isso cada resposta daqui carrega a cobertura, e a
tela que usar isto diz "provável" — a palavra não é gentileza, é a diferença
entre o técnico conferir e o técnico cavar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .plant_graph import CEO, CTO, Edge, Graph, Hop, NodeInput, NodeKey, route_to


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
    caixas: list[CaixaStatus] = field(default_factory=list)
    rota: list[Hop] = field(default_factory=list)
    # Cobertura: o mapa é menor que a massiva, e a tela declara isso.
    ctos_afetadas: int = 0
    ctos_no_grafo: int = 0

    # A partida foi confirmada por "todo mundo abaixo dela caiu"? Quando não,
    # o que se tem é o ponto comum das rotas — verdade menor, dita como tal.
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


def _descendentes(
    grafo: Graph, dist: dict[NodeKey, float], raiz: NodeKey
) -> set[NodeKey]:
    """Tudo que está **abaixo** da raiz, andando só para longe do POP.

    "Abaixo" é distância de cabo crescente. Sem essa regra a busca voltaria pelo
    tronco e diria que a cidade inteira pende de uma caixa de esquina.
    """
    vistos = {raiz}
    pilha = [raiz]
    saida: set[NodeKey] = set()
    while pilha:
        atual = pilha.pop()
        for aresta in grafo.vizinhos(atual):
            destino = aresta.destino
            if destino in vistos:
                continue
            if dist.get(destino, float("inf")) <= dist.get(atual, float("inf")):
                continue
            vistos.add(destino)
            saida.add(destino)
            pilha.append(destino)
    return saida


def _prefixo_comum(rotas: Sequence[list[Hop]]) -> list[Hop]:
    """O pedaço de rota que todas as CTOs afetadas compartilham.

    É onde o problema **tem** de estar: se duas CTOs caem e a fibra delas só se
    encontra no POP, ou são duas massivas ou o encontro é lá em cima.
    """
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


def calcular(
    grafo: Graph,
    dist: dict[NodeKey, float],
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    *,
    ctos_afetadas: Sequence[NodeKey],
    ctos_conhecidas: Sequence[NodeKey],
) -> RotaDoTecnico:
    """Monta a rota do técnico a partir das CTOs que caíram.

    `ctos_conhecidas` é a base contra a qual se decide quem **não** caiu. Sem
    ela, uma caixa com dois clientes e uma com noventa pareceriam iguais.
    """
    afetadas = {c for c in ctos_afetadas if c in dist}
    saida = RotaDoTecnico(
        ctos_afetadas=len(set(ctos_afetadas)),
        ctos_no_grafo=len(afetadas),
    )
    if not afetadas:
        return saida

    rotas = [route_to(grafo, dist, anterior, c) for c in sorted(afetadas)]
    rotas = [r for r in rotas if r]
    if not rotas:
        return saida

    comum = _prefixo_comum(rotas)
    if not comum:
        return saida

    saida.rota = comum
    saida.tem_origem_no_pop = comum[0].node.kind == "POP"

    conhecidas = set(ctos_conhecidas)
    caixas: list[CaixaStatus] = []
    for hop in comum:
        abaixo = _descendentes(grafo, dist, hop.node.key)
        ctos_abaixo = {k for k in abaixo if k[0] == CTO} & conhecidas
        if hop.node.key[0] == CTO:
            ctos_abaixo.add(hop.node.key)
        fora = tuple(
            grafo.nodes[k] for k in sorted(ctos_abaixo & afetadas) if k in grafo.nodes
        )
        no_ar = tuple(
            grafo.nodes[k] for k in sorted(ctos_abaixo - afetadas) if k in grafo.nodes
        )
        caixas.append(CaixaStatus(hop.node, hop.metros_do_pop, fora, no_ar))
    saida.caixas = caixas

    # O primeiro elemento da descida em que TODOS os clientes abaixo caíram.
    # Acima dele ainda há gente no ar, e é isso que fecha o trecho.
    primeiro = next((c for c in caixas if c.tudo_fora), None)
    saida.partida_confirmada = primeiro is not None
    if primeiro is None:
        # Nenhum ponto comum explica sozinho a massiva: o que se pode afirmar é
        # o ponto mais fundo compartilhado, sem chamá-lo de causa. Aconteceu em
        # produção com 2 CTOs fora e 35 no ar abaixo da mesma caixa — dizer
        # "rompimento aqui" ali seria mandar o técnico subir num poste bom.
        primeiro = caixas[-1]
    saida.partida = primeiro.node
    saida.partida_status = primeiro

    indice = caixas.index(primeiro)
    # O trecho (e o X que sai dele) só existe quando a partida foi confirmada E
    # o grafo tem massiva bastante para sustentar a conclusão.
    if indice > 0 and saida.partida_confirmada and saida.cobertura_suficiente:
        anterior_status = caixas[indice - 1]
        saida.caixa_anterior = anterior_status.node
        saida.trecho_rompido = (anterior_status.node, primeiro.node)
        # O cabo daquele pedaço: é o nome que o técnico procura no poste.
        passo = comum[indice]
        for aresta in grafo.vizinhos(anterior_status.node.key):
            if aresta.destino == primeiro.node.key and aresta.cabo_nome == passo.cabo_nome:
                saida.cabo_rompido = aresta
                break

    # A primeira CTO afetada é a que está mais perto do POP entre as que caíram.
    candidatas = [(dist[c], c) for c in afetadas]
    if candidatas:
        saida.primeira_cto = grafo.nodes[min(candidatas)[1]]

    return saida


def ceos_da_rota(rota: RotaDoTecnico) -> list[CaixaStatus]:
    """Só as caixas de emenda, que é o que o técnico baixa do poste."""
    return [c for c in rota.caixas if c.node.kind == CEO]
