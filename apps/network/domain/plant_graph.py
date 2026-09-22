"""O grafo da planta: quem vem antes de quem, seguindo o cabo.

Pedido de um técnico em 21/09/2026 (`docs/massivas-rota-do-tecnico-plano.md`):
ele não quer saber *quem caiu*, quer saber **por onde começar**. Isso exige uma
coisa que o sistema nunca teve — a noção de **ordem**: qual caixa está antes de
qual, saindo do POP.

O que existia era proximidade. "Esta emenda está a 80 m do evento" não diz se
ela alimenta as caixas que caíram ou se está do outro lado da rua, e mandar um
técnico baixar do poste a caixa errada custa uma subida inteira.

## O modelo, e por que ele é este

A planta vem de dois mundos que não se conhecem:

- o **cadastro FTTH** (`rad_caixa_ftth`), de onde saem as 1.445 CTOs com
  coordenada — é o que o login do cliente referencia;
- o **projeto do InMap** (`df_elemento`), de onde saem os 1.191 cabos com
  traçado e as 317 caixas de emenda.

Eles só se encontram por coordenada. E a medição em produção (21/09/2026) diz
como se encontram: **72% das CTOs estão a menos de 5 m de um vértice de cabo, e
quase nunca na ponta dele** — o cabo entra na caixa e segue. Por isso a aresta
não é "cabo liga ponta A a ponta B": o cabo é **cortado em todo elemento que
fica sobre o traçado**, e cada pedaço entre dois elementos consecutivos vira uma
aresta com o comprimento real daquele pedaço.

A diferença não é de estilo. Ligando só as pontas, o grafo alcançava 428 das
1.438 CTOs a partir de um POP. Cortando nos elementos do caminho, 977. Somando a
ligação exata por id de coordenada (ver `JUNCAO`), **1.055**.

## O que este módulo NÃO afirma

Tudo aqui é **inferência de cadastro**, não leitura da rede. O IXC não expõe a
fusão (que fibra de qual cabo entra em qual caixa); o que temos é o desenho e as
coordenadas. Duas caixas que o desenho põe no mesmo cabo podem estar em fibras
diferentes dele. A tela que usar este grafo declara isso, e por isso as funções
daqui devolvem sempre a cobertura junto com a resposta: quantos elementos
entraram no grafo e quantos ficaram de fora.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from .outage import haversine_meters

# Distância para considerar que um elemento está **sobre** o traçado do cabo.
#
# Medido, não escolhido: 72% das CTOs estão a ≤5 m de um vértice e 79% a ≤15 m.
# Subir para 60 m acrescenta 4 pontos de cobertura e começa a colar caixa em
# cabo de outra rua — e uma aresta errada aqui não é ruído: ela inverte o "antes
# e depois" de uma rota inteira.
TOLERANCIA_METROS = 15.0

# O POP é a exceção, e por um motivo físico: a coordenada cadastrada é a do
# prédio, e a fibra sai de uma caixa na calçada. Medido: com 15 m, nenhum cabo
# encostava em POP; com 300 m, três dos sete POPs entram no grafo.
TOLERANCIA_POP_METROS = 300.0

# Tipos de nó. São strings e não enum do Django de propósito: domínio não
# importa infraestrutura (AGENT.md §1.1).
POP = "POP"
CEO = "CEO"
CTO = "CTO"
# Ponto onde dois ou mais cabos compartilham a MESMA coordenada de origem. Não
# tem caixa cadastrada, mas tem fibra emendada: medido no projeto, são 1.420
# coordenadas ligando cabo a cabo. Sem este nó, dois cabos que se emendam no
# meio da rua ficam em pedaços separados do grafo — e era por isso que a rota
# não chegava ao POP em dois terços da planta.
JUNCAO = "JUNCAO"

NodeKey = tuple[str, str]


@dataclass(frozen=True)
class NodeInput:
    """Um elemento da planta que pode ser ponto de uma rota."""

    kind: str
    external_id: str
    label: str
    lat: float
    lon: float
    # Id da coordenada na origem, quando o elemento vem do projeto. É a ligação
    # EXATA: um cabo que tem este id entre seus vértices passa por dentro deste
    # elemento — não "perto dele". A CTO não tem (ela não existe no projeto) e
    # por isso continua sendo ligada por distância.
    coordinate_id: str = ""

    @property
    def key(self) -> NodeKey:
        return (self.kind, self.external_id)

    @property
    def point(self) -> tuple[float, float]:
        return (self.lat, self.lon)


@dataclass(frozen=True)
class CableInput:
    """O traçado de um cabo, vértice a vértice, na ordem do projeto."""

    external_id: str
    name: str
    type_name: str
    points: tuple[tuple[float, float], ...]
    # Id da coordenada de cada vértice, na mesma ordem. Vazio quando a origem
    # não expõe — e aí só resta a distância.
    coordinate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Edge:
    """Um pedaço de cabo entre dois elementos consecutivos do traçado."""

    destino: NodeKey
    metros: float
    cabo_id: str
    cabo_nome: str
    cabo_tipo: str

    @property
    def e_drop(self) -> bool:
        """Drop é a última perna — cabo de cliente, não tronco.

        O técnico pediu que eles entrem (o projeto tem drop ligando caixa a
        caixa), mas saber que um trecho é drop muda o que ele espera achar lá.
        """
        return "DROP" in (self.cabo_tipo or "").upper()


@dataclass
class Graph:
    """Nós e arestas da planta, mais o que ficou de fora."""

    nodes: dict[NodeKey, NodeInput] = field(default_factory=dict)
    edges: dict[NodeKey, list[Edge]] = field(default_factory=dict)
    # Cabos que não encostaram em dois elementos: não viram aresta, mas a
    # contagem vai para a tela. Um grafo com 20% da planta faltando e nenhum
    # aviso é pior que grafo nenhum.
    cabos_soltos: int = 0
    cabos_usados: int = 0

    def vizinhos(self, chave: NodeKey) -> list[Edge]:
        return self.edges.get(chave, [])


@dataclass(frozen=True)
class Hop:
    """Um passo da rota: o elemento e o cabo por onde se chegou nele."""

    node: NodeInput
    cabo_nome: str
    cabo_tipo: str
    metros_do_pop: float


def _celula(ponto: tuple[float, float], lado_graus: float) -> tuple[int, int]:
    return (int(ponto[0] / lado_graus), int(ponto[1] / lado_graus))


class _IndiceEspacial:
    """Busca do elemento mais próximo sem varrer os 1.700 a cada vértice.

    São ~11.600 vértices contra ~1.760 elementos: a varredura ingênua é
    20 milhões de distâncias por montagem do grafo. A grade de ~22 m resolve em
    uma fração disso, e o erro é zero porque a busca visita as células vizinhas.
    """

    # ~0,0002 grau ≈ 22 m na latitude de Sorocaba: uma célula por tolerância.
    LADO_GRAUS = 0.0002

    def __init__(self, nodes: Iterable[NodeInput]) -> None:
        self._grade: dict[tuple[int, int], list[NodeInput]] = {}
        for node in nodes:
            self._grade.setdefault(_celula(node.point, self.LADO_GRAUS), []).append(node)

    def mais_proximo(
        self, ponto: tuple[float, float], limite: float, *, kinds: Sequence[str] | None = None
    ) -> NodeInput | None:
        cx, cy = _celula(ponto, self.LADO_GRAUS)
        raio = 1 + int(limite / (self.LADO_GRAUS * 111_000))
        melhor: NodeInput | None = None
        menor = limite
        for dx in range(-raio, raio + 1):
            for dy in range(-raio, raio + 1):
                for node in self._grade.get((cx + dx, cy + dy), ()):
                    if kinds is not None and node.kind not in kinds:
                        continue
                    d = haversine_meters(ponto, node.point)
                    if d < menor:
                        melhor, menor = node, d
        return melhor


def build_graph(
    nodes: Sequence[NodeInput],
    cables: Sequence[CableInput],
    *,
    tolerancia: float = TOLERANCIA_METROS,
    tolerancia_pop: float = TOLERANCIA_POP_METROS,
) -> Graph:
    """Monta o grafo cortando cada cabo nos elementos que ficam sobre ele.

    Percorre os vértices **na ordem do traçado** e vai anotando quais elementos
    o cabo encosta. Entre dois elementos consecutivos nasce uma aresta, com o
    comprimento do pedaço de cabo entre eles — não a reta, que num cabo que
    contorna quarteirão erra por centenas de metros.

    A aresta é de mão dupla: o grafo não sabe sozinho para que lado a luz anda.
    Quem dá o sentido é `distances_from`, partindo do POP.
    """
    grafo = Graph(nodes={n.key: n for n in nodes})
    indice = _IndiceEspacial(nodes)

    # Ligação EXATA: elemento do projeto ↔ vértice de cabo pelo id da
    # coordenada. É a mesma linha de `df_coordenada` nos dois, não dois pontos
    # parecidos.
    por_coordenada: dict[str, NodeInput] = {
        n.coordinate_id: n for n in nodes if n.coordinate_id
    }

    # Junções: coordenada usada por dois ou mais CABOS. Ali a fibra é emendada
    # mesmo sem caixa cadastrada, e sem um nó o grafo se parte em dois.
    uso: dict[str, list[tuple[CableInput, int]]] = {}
    for cabo in cables:
        for i, coord in enumerate(cabo.coordinate_ids):
            if coord:
                uso.setdefault(coord, []).append((cabo, i))
    for coord, ocorrencias in uso.items():
        if coord in por_coordenada or len({c.external_id for c, _ in ocorrencias}) < 2:
            continue
        cabo, i = ocorrencias[0]
        if i >= len(cabo.points):
            continue
        lat, lon = cabo.points[i]
        node = NodeInput(JUNCAO, f"coord-{coord}", "Junção de cabos", lat, lon, coord)
        grafo.nodes[node.key] = node
        por_coordenada[coord] = node

    for cabo in cables:
        if len(cabo.points) < 2:
            continue
        na_rota: list[tuple[NodeInput, float]] = []
        acumulado = 0.0
        for i, vertice in enumerate(cabo.points):
            if i:
                acumulado += haversine_meters(cabo.points[i - 1], vertice)
            # Um vértice pode hospedar DOIS elementos, e os dois valem: a
            # junção gravada na coordenada (leitura) e a CTO a poucos metros
            # dela (inferência), que é o caso comum de uma caixa de cliente
            # pendurada na emenda do poste.
            #
            # A primeira versão tratava os dois como alternativa — o exato
            # ganhava e o próximo era descartado —, e a cobertura CAIU de 977
            # para 817 CTOs: as caixas que dividiam poste com uma junção
            # sumiam do grafo. Os dois entram, o exato primeiro.
            coord = cabo.coordinate_ids[i] if i < len(cabo.coordinate_ids) else ""
            achados: list[NodeInput] = []
            exato = por_coordenada.get(coord) if coord else None
            if exato is not None:
                achados.append(exato)
            perto = indice.mais_proximo(vertice, tolerancia)
            if perto is None and exato is None and tolerancia_pop > tolerancia:
                # O POP tem alcance próprio (ver TOLERANCIA_POP_METROS) — mas
                # só quando o vértice está órfão. Aplicá-lo também onde já há
                # elemento faria cada vértice a 300 m do POP virar uma ligação
                # direta com ele, encurtando rotas que na fibra não existem.
                perto = indice.mais_proximo(vertice, tolerancia_pop, kinds=(POP,))
            if perto is not None and (exato is None or perto.key != exato.key):
                achados.append(perto)

            for achado in achados:
                if na_rota and na_rota[-1][0].key == achado.key:
                    # O mesmo elemento encostando em vértices seguidos é um
                    # ponto só: contar duas vezes criaria aresta de zero metro.
                    continue
                na_rota.append((achado, acumulado))

        if len(na_rota) < 2:
            grafo.cabos_soltos += 1
            continue

        grafo.cabos_usados += 1
        for (a, da), (b, db) in pairwise(na_rota):
            # Um metro de piso: dois elementos no mesmo vértice existem (caixa
            # dupla no poste), e aresta de peso zero faria o Dijkstra empatar
            # rotas que não são a mesma.
            metros = max(abs(db - da), 1.0)
            grafo.edges.setdefault(a.key, []).append(
                Edge(b.key, metros, cabo.external_id, cabo.name, cabo.type_name)
            )
            grafo.edges.setdefault(b.key, []).append(
                Edge(a.key, metros, cabo.external_id, cabo.name, cabo.type_name)
            )
    return grafo


def distances_from(
    grafo: Graph, origens: Sequence[NodeKey]
) -> tuple[dict[NodeKey, float], dict[NodeKey, tuple[NodeKey, Edge]]]:
    """Dijkstra: metros de cabo de cada elemento até a origem mais próxima.

    É esta função que dá **sentido** ao grafo. "Antes" e "depois" não existem no
    desenho — a ordem dos vértices do IXC não serve para isso (medido: 654 cabos
    começam mais perto do POP e 513 terminam). O que define o sentido é a
    distância de cabo até o POP.

    Devolve `(distancias, anterior)`; `anterior` reconstrói a rota.
    """
    dist: dict[NodeKey, float] = {}
    anterior: dict[NodeKey, tuple[NodeKey, Edge]] = {}
    fila: list[tuple[float, NodeKey]] = []
    for chave in origens:
        if chave in grafo.nodes:
            dist[chave] = 0.0
            heapq.heappush(fila, (0.0, chave))

    while fila:
        d, atual = heapq.heappop(fila)
        if d > dist.get(atual, float("inf")):
            continue
        for aresta in grafo.vizinhos(atual):
            novo = d + aresta.metros
            if novo < dist.get(aresta.destino, float("inf")):
                dist[aresta.destino] = novo
                anterior[aresta.destino] = (atual, aresta)
                heapq.heappush(fila, (novo, aresta.destino))
    return dist, anterior


def route_to(
    grafo: Graph,
    dist: dict[NodeKey, float],
    anterior: dict[NodeKey, tuple[NodeKey, Edge]],
    destino: NodeKey,
) -> list[Hop]:
    """A rota do POP até o elemento, do POP para o cliente.

    Lista vazia quando o elemento não é alcançável — e isso **não** é erro: é o
    estado de 30% das CTOs hoje, e quem chama declara a cobertura em vez de
    fingir uma rota.
    """
    if destino not in dist:
        return []
    passos: list[Hop] = []
    atual = destino
    while atual in anterior:
        pai, aresta = anterior[atual]
        node = grafo.nodes[atual]
        passos.append(Hop(node, aresta.cabo_nome, aresta.cabo_tipo, dist[atual]))
        atual = pai
    passos.append(Hop(grafo.nodes[atual], "", "", dist.get(atual, 0.0)))
    passos.reverse()
    return passos
