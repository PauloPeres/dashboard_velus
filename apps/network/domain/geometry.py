"""Geometria da planta — distância até um traçado e escolha do cabo candidato.

Domínio puro: sem Django, sem banco. Entra lista de pontos, sai distância em
metros e uma lista ordenada de candidatos.

**O que este módulo pode afirmar:** que um cabo cadastrado passa a tantos metros
de uma caixa afetada. **O que ele não pode:** que a fibra passa exatamente ali,
que aquele cabo alimenta aquela caixa, ou que foi ele que rompeu. O traçado é
desenho de projeto (InMap), e o vínculo é proximidade — não é um campo de
cadastro ligando cabo a CTO, porque esse campo não existe.

*Medido em produção (2026-09-19):* 1.120 das 1.431 CTOs com coordenada estão a
≤10 m de um vértice de cabo e a mediana da distância é 0,0 m — o InMap usa o
mesmo ponto da caixa. A ≤30 m são 1.211. Os ~15% restantes são caixas sem cabo
cadastrado por perto, e para elas a resposta certa é dizer que não há candidato.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import cos, radians
from urllib.parse import unquote

from .outage import haversine_meters

# Raio em que um cabo ainda é candidato. Sai da medição: a ≤30 m estão 1.211 das
# 1.431 caixas, e o p90 é 81,5 m. Esticar até o p90 traria, para as caixas mal
# cadastradas, o cabo de outra rua — que é pior que dizer "sem candidato aqui".
RAIO_CANDIDATO_METROS = 30.0

# Classes lidas do nome do cabo (§1a do plano de rota e cabos). O IXC não tem
# campo de classe; a convenção de nomenclatura do cadastro tem.
CLASSE_BACKBONE = "BACKBONE"
CLASSE_ATENDIMENTO = "ATENDIMENTO"
CLASSE_DROP = "DROP"


@dataclass(frozen=True)
class PathInput:
    """Um traçado cadastrado: id, nome, tipo e os pontos em ordem."""

    external_id: str
    name: str
    points: Sequence[tuple[float, float]]
    project_external_id: str = ""
    # Nome do tipo na origem. É ele que responde "que cabo é este" — ver
    # `cable_class`.
    type_name: str = ""


@dataclass(frozen=True)
class CableCandidate:
    """Um cabo que passa perto das caixas do evento — candidato, não culpado."""

    external_id: str
    name: str
    classe: str
    distance_meters: float
    # Quantas das caixas afetadas este cabo toca. Um cabo que passa por cinco das
    # seis caixas fora é uma pista melhor que o que encosta em uma só.
    ctos_tocadas: int
    project_external_id: str = ""


def cable_class(name: str, type_name: str = "") -> str:
    """Classe do cabo — do **tipo** primeiro, do nome só como reserva.

    O tipo é o campo que responde de verdade. *Medido em produção
    (2026-09-19):* o `nome_tipo` do catálogo do InMap classifica **1.092 dos
    1.191 cabos** (843 atendimento, 125 drop, 124 backbone), enquanto a
    descrição do próprio cabo só classifica 47% deles.

    E a diferença não é cosmética: **57 cabos cujo tipo é "CLIENTE DROP 1FO" se
    chamam apenas "01FO"**. Lidos pelo nome, entravam como candidatos a explicar
    uma massiva de trinta clientes — que é precisamente o que um drop de um
    filamento não pode fazer.

    O nome continua como reserva para o cabo sem tipo cadastrado (11 em
    produção). Quando nem um nem outro dizem, devolve "" — e a tela declara que
    o cadastro não conta, em vez de deduzir classe pela capacidade.
    """
    for texto in ((type_name or "").upper(), (name or "").upper()):
        if not texto:
            continue
        # A ordem importa: drop é o que será descartado, então é o que precisa
        # ser reconhecido primeiro.
        if CLASSE_DROP in texto:
            return CLASSE_DROP
        if CLASSE_BACKBONE in texto:
            return CLASSE_BACKBONE
        if CLASSE_ATENDIMENTO in texto:
            return CLASSE_ATENDIMENTO
    return ""


def distance_to_path(
    point: tuple[float, float], path: Sequence[tuple[float, float]]
) -> float:
    """Menor distância, em metros, do ponto até a polilinha.

    Mede contra os **segmentos**, não só contra os vértices: um cabo com dois
    vértices a 200 m um do outro passa a poucos metros de uma caixa no meio do
    caminho, e medir só vértice diria 100 m. Como os segmentos são curtos
    (mediana de 5 vértices por cabo), a projeção local plana é suficiente —
    trabalhar em coordenadas geodésicas aqui custaria mais e mudaria a resposta
    em centímetros.
    """
    if not path:
        return float("inf")
    if len(path) == 1:
        return haversine_meters(point, path[0])

    return min(_distance_to_segment(point, a, b) for a, b in pairwise(path))


def _distance_to_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """Distância ponto→segmento por projeção local (metros)."""
    # Metros por grau na latitude do ponto: longitude encolhe com o cosseno, e
    # ignorar isso erraria ~8% na latitude de Sorocaba.
    lat_ref = radians(point[0])
    m_por_grau_lat = 111_132.0
    m_por_grau_lon = 111_320.0 * cos(lat_ref)

    def para_metros(p: tuple[float, float]) -> tuple[float, float]:
        return (
            (p[0] - point[0]) * m_por_grau_lat,
            (p[1] - point[1]) * m_por_grau_lon,
        )

    ax, ay = para_metros(start)
    bx, by = para_metros(end)
    dx, dy = bx - ax, by - ay
    comprimento2 = dx * dx + dy * dy
    if comprimento2 == 0:
        return (ax * ax + ay * ay) ** 0.5
    # t é onde a perpendicular cai; fora de [0, 1] o mais próximo é a ponta.
    t = -(ax * dx + ay * dy) / comprimento2
    t = max(0.0, min(1.0, t))
    px, py = ax + dx * t, ay + dy * t
    return (px * px + py * py) ** 0.5


def candidate_cables(
    cto_points: Sequence[tuple[float, float]],
    paths: Sequence[PathInput],
    *,
    radius_meters: float = RAIO_CANDIDATO_METROS,
    incluir_drop: bool = False,
) -> list[CableCandidate]:
    """Cabos que passam a ≤`radius_meters` de alguma das caixas afetadas.

    Ordena por quantas caixas o cabo toca (desc) e depois pela distância: o cabo
    que costura cinco das seis caixas fora é a pista, mesmo que outro encoste
    mais perto de uma delas.

    **Drop fica de fora por padrão.** Drop é o pedaço que vai do poste à casa de
    um cliente: ele nunca explica trinta clientes fora, e listá-lo enterraria os
    dois cabos que importam sob dezenas de nomes irrelevantes.
    """
    if not cto_points or not paths:
        return []

    candidatos: list[CableCandidate] = []
    for path in paths:
        classe = cable_class(path.name, path.type_name)
        if classe == CLASSE_DROP and not incluir_drop:
            continue
        # Só as caixas dentro da caixa envolvente do cabo são medidas. As de
        # fora estão a mais que o raio — não tocam, e a menor distância que
        # importa é a de uma que toca. Mesmo resultado, sem a conta cara.
        caixa = _caixa_envolvente(path.points, radius_meters)
        perto = [p for p in cto_points if _dentro(p, caixa)]
        if not perto:
            continue
        distancias = [distance_to_path(p, path.points) for p in perto]
        tocadas = sum(1 for d in distancias if d <= radius_meters)
        if not tocadas:
            continue
        candidatos.append(
            CableCandidate(
                external_id=path.external_id,
                name=path.name,
                classe=classe,
                distance_meters=round(min(distancias), 1),
                ctos_tocadas=tocadas,
                project_external_id=path.project_external_id,
            )
        )
    candidatos.sort(key=lambda c: (-c.ctos_tocadas, c.distance_meters, c.name))
    return candidatos


def ctos_sem_cabo(
    cto_points: Sequence[tuple[float, float]],
    paths: Sequence[PathInput],
    *,
    radius_meters: float = RAIO_CANDIDATO_METROS,
) -> int:
    """Quantas caixas do evento não têm cabo cadastrado dentro do raio.

    É esta contagem que a tela declara. Sem ela, uma lista curta de candidatos
    pareceria "achamos pouco" quando o caso é "metade das caixas não tem cabo
    cadastrado perto" — dois estados diferentes que levam a ações diferentes.
    """
    if not cto_points:
        return 0
    if not paths:
        return len(cto_points)
    caixas = [(_caixa_envolvente(path.points, radius_meters), path.points) for path in paths]
    return sum(
        1
        for p in cto_points
        if not any(
            _dentro(p, caixa) and distance_to_path(p, pontos) <= radius_meters
            for caixa, pontos in caixas
        )
    )


def _caixa_envolvente(
    points: Sequence[tuple[float, float]], margem_metros: float
) -> tuple[float, float, float, float]:
    """(lat_min, lat_max, lon_min, lon_max) do traçado, alargada pela margem.

    É o filtro que tira a medida ponto-segmento do caminho quente: numa massiva
    de OLT (220 caixas × 1.191 cabos) eram 260 mil medidas e 12 s por página
    (30/09/2026, massiva 359). O que está fora da caixa está a mais que a margem
    do cabo e não precisa ser medido.

    A margem vai 50% maior que o raio: a longitude encolhe com o cosseno da
    latitude, e a medida exata usa o cosseno do ponto, não o do cabo. A folga
    garante que o filtro nunca descarta o que a medida aceitaria.
    """
    if not points:
        return (0.0, -1.0, 0.0, -1.0)
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    folga = margem_metros * 1.5
    graus_lat = folga / 111_132.0
    graus_lon = folga / (111_320.0 * max(cos(radians((min(lats) + max(lats)) / 2)), 0.1))
    return (
        min(lats) - graus_lat,
        max(lats) + graus_lat,
        min(lons) - graus_lon,
        max(lons) + graus_lon,
    )


def _dentro(p: tuple[float, float], caixa: tuple[float, float, float, float]) -> bool:
    return caixa[0] <= p[0] <= caixa[1] and caixa[2] <= p[1] <= caixa[3]


def sub_path_between(
    path: Sequence[tuple[float, float]],
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    radius_meters: float = RAIO_CANDIDATO_METROS,
) -> list[tuple[float, float]]:
    """O pedaço do traçado entre os dois pontos — [] se o cabo não serve aos dois.

    É o que transforma "trecho suspeito" de reta entre caixas em **pedaço de
    cabo**: se existe um cabo que passa pelas duas caixas, o trecho entre elas é
    o caminho que a fibra faz, com as curvas do projeto, e não a linha reta que
    atravessa quarteirão.

    Devolve vazio quando alguma das pontas está longe do cabo: um "trecho" que
    começa a 300 m da caixa não é o caminho dela, e desenhá-lo mandaria o
    técnico para a rua errada — exatamente o erro que a regra de não prometer
    traçado existia para evitar.

    As pontas do recorte são os vértices, não a projeção exata sobre o segmento:
    a diferença é de metros, e vértice é ponto que existe no cadastro.
    """
    if len(path) < 2:
        return []
    if distance_to_path(a, path) > radius_meters:
        return []
    if distance_to_path(b, path) > radius_meters:
        return []

    def vertice_mais_perto(ponto: tuple[float, float]) -> int:
        return min(
            range(len(path)),
            key=lambda i: _distance_to_segment(ponto, path[i], path[i]),
        )

    i, j = vertice_mais_perto(a), vertice_mais_perto(b)
    if i == j:
        # As duas caixas caem no mesmo vértice: não há pedaço de cabo entre
        # elas, e devolver um ponto só desenharia nada.
        return []
    inicio, fim = (i, j) if i < j else (j, i)
    return list(path[inicio : fim + 1])


def ponto_no_meio(path: Sequence[tuple[float, float]]) -> tuple[float, float] | None:
    """O ponto a meio caminho do traçado, medido pelo comprimento.

    Não é o vértice do meio: num pedaço de cabo com vértices amontoados de um
    lado (uma esquina desenhada com cinco pontos), o vértice do meio cai na
    esquina, longe da metade do caminho que o técnico vai andar.
    """
    if not path:
        return None
    if len(path) == 1:
        return path[0]
    trechos = [haversine_meters(a, b) for a, b in pairwise(path)]
    falta = sum(trechos) / 2
    for (a, b), comprimento in zip(pairwise(path), trechos, strict=True):
        if comprimento > 0 and falta <= comprimento:
            t = falta / comprimento
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        falta -= comprimento
    return path[-1]


# Formas em que uma coordenada chega colada: do mais preciso para o mais solto.
# O `!3d…!4d…` é o alfinete de um lugar no Google Maps; o `@lat,lon` é o centro
# da tela (menos preciso: é onde a pessoa estava olhando); `q=`/`query=` é o
# link de busca; e por fim "lat, long" solto, digitado ou copiado do app.
_PADROES_DE_COORDENADA = (
    re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)"),
    re.compile(
        r"[?&](?:q|query|ll|destination|center)=(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
    ),
    re.compile(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)"),
    re.compile(r"(-?\d{1,2}\.\d+)\s*[,;\s]\s*(-?\d{1,3}\.\d+)"),
)


def coordenadas_do_texto(texto: str) -> tuple[float, float] | None:
    """Lat/long de um link do Google Maps ou de "lat, long" colado.

    Pedido de 30/09/2026: o local do rompimento da massiva 359 chegou como link
    do Google Maps numa mensagem. Quem registra o rompimento cola o que tem na
    mão — obrigar a separar latitude e longitude em dois campos é o tipo de
    atrito que faz o campo ficar vazio.

    Link encurtado (`maps.app.goo.gl/...`) não traz a coordenada no texto e
    devolve None: resolvê-lo exigiria chamar o Google, e a tela pede o link
    completo ou o par de números.
    """
    bruto = unquote((texto or "").strip())
    for padrao in _PADROES_DE_COORDENADA:
        achado = padrao.search(bruto)
        if not achado:
            continue
        lat, lon = float(achado.group(1)), float(achado.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
            return (lat, lon)
    return None
