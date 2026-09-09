"""Detector de quedas massivas — núcleo puro, sem Django.

Implementa a regra de `docs/massivas-plano.md` §5: agrupa quedas por janela de
tempo, decide o escopo pela topologia (CTO → PON → OLT → POP) e, quando nada de
topologia fecha, tenta o agrupamento geográfico por haversine.

O módulo é deliberadamente autossuficiente (define seus próprios dataclasses de
entrada e saída) para poder ser testado sem banco, sem ORM e sem adapter.

Limite herdado de §2.3: a geometria de cabo não é acessível pela API, então o
detector nunca afirma qual cabo rompeu — no máximo aponta o *trecho suspeito*
entre duas caixas.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

__all__ = [
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "CONFIDENCE_MEDIUM",
    "SCOPE_CTO",
    "SCOPE_GEO",
    "SCOPE_OLT",
    "SCOPE_PON",
    "SCOPE_POP",
    "DropInput",
    "OutageCluster",
    "TopologyInput",
    "detect_outages",
]

SCOPE_CTO = "CTO"
SCOPE_PON = "PON"
SCOPE_OLT = "OLT"
SCOPE_POP = "POP"
SCOPE_GEO = "GEO"

CONFIDENCE_HIGH = "ALTA"
CONFIDENCE_MEDIUM = "MEDIA"
CONFIDENCE_LOW = "BAIXA"

# Fração a partir da qual o escopo fechado por topologia é considerado ALTA (§5.5).
_HIGH_CONFIDENCE_FRACTION = 0.90

# Denominador mínimo pra uma CTO servir de EVIDÊNCIA de escopo. Medição na base
# de produção (2026-09-08, 852 CTOs com login ativo): 307 têm 1 login, 204 têm 2
# — 511 das 852 têm no máximo 2. Uma CTO de 1 login que cai fica "100% fora" e
# passa trivialmente em qualquer limiar de fração sem ser evidência nenhuma de
# rompimento. Fração alta sobre denominador pequeno é aritmética, não sinal.
_DEFAULT_MIN_CTO_DENOMINATOR = 3

# Piso de denominador pra afirmar ALTA confiança. Deliberadamente independente do
# knob de escopo: afrouxar o limiar de escopo é decisão operacional, mas dizer
# "ALTA confiança" sobre 1 ou 2 logins nunca é honesto.
_MIN_DENOMINATOR_FOR_HIGH_CONFIDENCE = 3

# Quantas caixas o rótulo do trecho nomeia antes de resumir o resto em contagem.
_MAX_NAMED_SEGMENT_CTOS = 3

# Raio médio da Terra em metros — haversine na mão, sem dependência nova.
_EARTH_RADIUS_METERS = 6_371_000.0


@dataclass(frozen=True)
class DropInput:
    """Uma queda individual já registrada.

    `pon_id` é propriedade DO LOGIN, não da caixa: medido no IXC em 2026-09-08,
    a porta vem de `radpop_radio_cliente_fibra.id_radpop_radio_porta` (preenchida
    em 4.553 de 4.559 registros), e ao derivar um mapa CTO → PON 239 das 927 CTOs
    deriváveis (26%) apontam pra mais de uma porta. Uma mesma caixa pode ser
    alimentada por mais de uma PON. Por isso a PON mora aqui, por queda, e nunca
    deve ser deduzida da CTO do login.
    """

    login_id: str
    dropped_at: datetime
    cto_id: str = ""
    cto_port: str = ""
    transmitter_id: str = ""
    pon_id: str = ""
    pop_id: str = ""
    latitude: float | None = None
    longitude: float | None = None
    reason: str = ""
    monthly_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class TopologyInput:
    """Denominadores da topologia: quantos logins ATIVOS cada elemento tem."""

    active_logins_per_cto: Mapping[str, int] = field(default_factory=dict)
    # Mantido pelo contrato, mas NÃO decide a PON de nenhum login: 26% das CTOs
    # são alimentadas por mais de uma porta (ver docstring de DropInput). Serve
    # só como sinal auxiliar de cadastro; o detector deriva PON dos logins.
    cto_to_pon: Mapping[str, str] = field(default_factory=dict)
    # `id_transmissor` vem preenchido em 100% das CTOs — este mapa é confiável.
    cto_to_transmitter: Mapping[str, str] = field(default_factory=dict)
    cto_to_pop: Mapping[str, str] = field(default_factory=dict)
    cto_coordinates: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    cto_names: Mapping[str, str] = field(default_factory=dict)
    # Extensão opcional ao contrato: sem a posição do POP não há como dizer qual
    # CTO é a mais próxima dele (§5.4). Vazio, o rótulo do trecho ainda sai, mas
    # em ordem determinística em vez de topológica.
    pop_coordinates: Mapping[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class OutageCluster:
    scope: str
    started_at: datetime
    element_id: str
    element_label: str
    suspected_segment_label: str
    confidence: str
    login_ids: tuple[str, ...]
    affected_count: int
    mrr_at_risk: Decimal
    affected_fraction: float


def detect_outages(
    drops: Sequence[DropInput],
    topology: TopologyInput,
    *,
    window_minutes: int = 10,
    min_clients: int = 5,
    cto_fraction_threshold: float = 0.70,
    min_cto_denominator: int = _DEFAULT_MIN_CTO_DENOMINATOR,
    geo_radius_meters: float = 300.0,
) -> list[OutageCluster]:
    """Agrupa quedas em massivas. Devolve lista estável e ordenada.

    Quedas que não entram em nenhum cluster simplesmente não aparecem na saída —
    quem chama continua tendo o evento individual registrado (§5.6).
    """
    if window_minutes <= 0:
        raise ValueError("window_minutes precisa ser positivo")
    if min_clients <= 0:
        raise ValueError("min_clients precisa ser positivo")

    resolved = _ResolvedTopology.build(drops, topology)
    clusters: list[OutageCluster] = []
    for window in _split_windows(_dedupe_by_login(drops), window_minutes):
        clusters.extend(
            _detect_in_window(
                window,
                resolved,
                min_clients=min_clients,
                cto_fraction_threshold=cto_fraction_threshold,
                min_cto_denominator=min_cto_denominator,
                geo_radius_meters=geo_radius_meters,
            )
        )

    # A UI e os testes dependem desta ordem — não trocar sem alinhar os dois.
    clusters.sort(key=lambda c: (c.started_at, -c.affected_count, c.element_id))
    return clusters


# ---------------------------------------------------------------------------
# Topologia resolvida
# ---------------------------------------------------------------------------


class _ResolvedTopology:
    """Topologia com os buracos preenchidos pelo snapshot das próprias quedas.

    O `ConnectionDropEvent` guarda a topologia do momento da queda; quando o
    cadastro da CTO ainda não foi sincronizado, esse snapshot é a única fonte.
    """

    def __init__(
        self,
        *,
        active_per_cto: Mapping[str, int],
        cto_to_transmitter: Mapping[str, str],
        cto_to_pop: Mapping[str, str],
        cto_coordinates: Mapping[str, tuple[float, float]],
        cto_names: Mapping[str, str],
        pop_coordinates: Mapping[str, tuple[float, float]],
    ) -> None:
        self.active_per_cto = active_per_cto
        self.cto_to_transmitter = cto_to_transmitter
        self.cto_to_pop = cto_to_pop
        self.cto_coordinates = cto_coordinates
        self.cto_names = cto_names
        self.pop_coordinates = pop_coordinates

    @classmethod
    def build(cls, drops: Sequence[DropInput], topology: TopologyInput) -> _ResolvedTopology:
        cto_to_transmitter = dict(topology.cto_to_transmitter)
        cto_to_pop = dict(topology.cto_to_pop)
        for drop in drops:
            if not drop.cto_id:
                continue
            if not cto_to_transmitter.get(drop.cto_id) and drop.transmitter_id:
                cto_to_transmitter[drop.cto_id] = drop.transmitter_id
            if not cto_to_pop.get(drop.cto_id) and drop.pop_id:
                cto_to_pop[drop.cto_id] = drop.pop_id
        return cls(
            active_per_cto=dict(topology.active_logins_per_cto),
            cto_to_transmitter=cto_to_transmitter,
            cto_to_pop=cto_to_pop,
            cto_coordinates=dict(topology.cto_coordinates),
            cto_names=dict(topology.cto_names),
            pop_coordinates=dict(topology.pop_coordinates),
        )

    def active_logins(self, cto_id: str) -> int:
        return int(self.active_per_cto.get(cto_id, 0))

    def transmitter_of(self, cto_id: str) -> str:
        return self.cto_to_transmitter.get(cto_id, "")

    def pop_of(self, cto_id: str) -> str:
        return self.cto_to_pop.get(cto_id, "")

    def cto_label(self, cto_id: str) -> str:
        return self.cto_names.get(cto_id) or cto_id

    def ctos_under(self, level: str, element_id: str) -> list[str]:
        """CTOs cadastradas sob uma OLT/POP — base do denominador desses escopos."""
        mapping = {SCOPE_OLT: self.cto_to_transmitter, SCOPE_POP: self.cto_to_pop}[level]
        return sorted(cto for cto, parent in mapping.items() if parent == element_id)


# ---------------------------------------------------------------------------
# Janelas
# ---------------------------------------------------------------------------


def _dedupe_by_login(drops: Sequence[DropInput]) -> list[DropInput]:
    """Um login entra no detector uma única vez (§5.6), pela queda mais antiga."""
    earliest: dict[str, DropInput] = {}
    for drop in drops:
        current = earliest.get(drop.login_id)
        if current is None or drop.dropped_at < current.dropped_at:
            earliest[drop.login_id] = drop
    return sorted(earliest.values(), key=lambda d: (d.dropped_at, d.login_id))


def _split_windows(drops: Sequence[DropInput], window_minutes: int) -> list[list[DropInput]]:
    """Fatia as quedas em janelas ancoradas na primeira queda de cada janela.

    A âncora (em vez de encadear quedas consecutivas) impede que um gotejamento
    de quedas independentes, cada uma poucos minutos após a anterior, se cole num
    cluster arbitrariamente longo.
    """
    windows: list[list[DropInput]] = []
    span = timedelta(minutes=window_minutes)
    current: list[DropInput] = []
    anchor: datetime | None = None
    for drop in drops:
        if anchor is None or drop.dropped_at - anchor > span:
            if current:
                windows.append(current)
            current = [drop]
            anchor = drop.dropped_at
        else:
            current.append(drop)
    if current:
        windows.append(current)
    return windows


# ---------------------------------------------------------------------------
# Detecção dentro de uma janela
# ---------------------------------------------------------------------------


def _detect_in_window(
    drops: Sequence[DropInput],
    topology: _ResolvedTopology,
    *,
    min_clients: int,
    cto_fraction_threshold: float,
    min_cto_denominator: int,
    geo_radius_meters: float,
) -> list[OutageCluster]:
    clusters = _topological_clusters(
        drops,
        topology,
        min_clients=min_clients,
        cto_fraction_threshold=cto_fraction_threshold,
        min_cto_denominator=min_cto_denominator,
    )
    claimed = {login for cluster in clusters for login in cluster.login_ids}
    leftovers = [d for d in drops if d.login_id not in claimed]
    clusters.extend(
        _geo_clusters(
            leftovers,
            topology,
            min_clients=min_clients,
            geo_radius_meters=geo_radius_meters,
        )
    )
    return clusters


def _qualified_ctos(
    drops_by_cto: Mapping[str, list[DropInput]],
    topology: _ResolvedTopology,
    *,
    cto_fraction_threshold: float,
    min_cto_denominator: int,
) -> list[str]:
    """CTOs que servem de evidência: denominador suficiente E fração acima do limiar.

    Logins de CTOs reprovadas aqui continuam entrando no cluster (contam em
    `affected_count` e no MRR) — elas só não sustentam a afirmação "esta caixa
    caiu", e portanto não alimentam a escalada pra PON/OLT/POP.
    """
    qualified = []
    for cto_id, cto_drops in drops_by_cto.items():
        active = topology.active_logins(cto_id)
        if active < max(1, min_cto_denominator):
            continue
        if len(cto_drops) / active >= cto_fraction_threshold:
            qualified.append(cto_id)
    return sorted(qualified)


def _pon_of_cto(cto_drops: Sequence[DropInput]) -> str:
    """PON da CTO na janela: a porta que mais logins caídos dela apontam.

    Uma caixa pode legitimamente pertencer a duas PONs. Para que um login caia em
    no máximo um cluster (§5.6), a CTO é filiada à porta majoritária entre as
    suas próprias quedas; empate resolve pelo menor id, pra saída determinística.
    Sem `pon_id` em nenhuma queda, devolve "" e o degrau de PON simplesmente não
    fecha — a decisão cai pro degrau de OLT.
    """
    tally: dict[str, int] = defaultdict(int)
    for drop in cto_drops:
        if drop.pon_id:
            tally[drop.pon_id] += 1
    if not tally:
        return ""
    return min(tally, key=lambda pon: (-tally[pon], pon))


def _topological_clusters(
    drops: Sequence[DropInput],
    topology: _ResolvedTopology,
    *,
    min_clients: int,
    cto_fraction_threshold: float,
    min_cto_denominator: int,
) -> list[OutageCluster]:
    drops_by_cto: dict[str, list[DropInput]] = defaultdict(list)
    for drop in drops:
        if drop.cto_id:
            drops_by_cto[drop.cto_id].append(drop)

    qualified = _qualified_ctos(
        drops_by_cto,
        topology,
        cto_fraction_threshold=cto_fraction_threshold,
        min_cto_denominator=min_cto_denominator,
    )
    cto_pon = {cto: _pon_of_cto(drops_by_cto[cto]) for cto in qualified}

    # Degrau PON: ≥2 CTOs em escopo filiadas à mesma porta.
    pon_members: dict[str, list[str]] = defaultdict(list)
    for cto in qualified:
        if cto_pon[cto]:
            pon_members[cto_pon[cto]].append(cto)
    pon_groups = {pon: ctos for pon, ctos in pon_members.items() if len(ctos) >= 2}

    # Degrau OLT: ≥2 unidades de evidência no mesmo transmissor. Unidade é uma
    # porta PON com CTO em escopo OU — quando o login não traz porta — a própria
    # CTO em escopo. É o que faz o degrau degradar sem quebrar com `pon_id` vazio.
    olt_units: dict[str, set[str]] = defaultdict(set)
    for cto in qualified:
        transmitter = topology.transmitter_of(cto)
        if not transmitter:
            continue
        olt_units[transmitter].add(f"PON:{cto_pon[cto]}" if cto_pon[cto] else f"CTO:{cto}")
    olts_in_scope = sorted(olt for olt, units in olt_units.items() if len(units) >= 2)

    # Degrau POP: ≥2 OLTs em escopo no mesmo POP.
    pop_members: dict[str, set[str]] = defaultdict(set)
    for olt in olts_in_scope:
        for cto in topology.ctos_under(SCOPE_OLT, olt):
            if topology.pop_of(cto):
                pop_members[topology.pop_of(cto)].add(olt)
                break
    pops_in_scope = sorted(pop for pop, olts in pop_members.items() if len(olts) >= 2)

    plan: list[tuple[str, str, list[str]]] = []
    plan += [(SCOPE_POP, pop, sorted(pop_members[pop])) for pop in pops_in_scope]
    plan += [(SCOPE_OLT, olt, sorted(olt_units[olt])) for olt in olts_in_scope]
    plan += [(SCOPE_PON, pon, sorted(ctos)) for pon, ctos in sorted(pon_groups.items())]
    plan += [(SCOPE_CTO, cto, [cto]) for cto in qualified]

    clusters: list[OutageCluster] = []
    used_logins: set[str] = set()
    used_evidence: set[str] = set()
    for scope, element_id, evidence in plan:
        # A escalada vence: quem já entrou numa massiva mais alta não vira massiva
        # própria de novo, e o elemento mais genérico é o que aponta o suspeito.
        if any(item in used_evidence for item in evidence):
            continue
        members = [
            d
            for d in _members_for(scope, element_id, drops, drops_by_cto, cto_pon, topology)
            if d.login_id not in used_logins
        ]
        if len(members) < min_clients:
            continue
        denominator = _denominator_for(scope, element_id, cto_pon, topology)
        clusters.append(
            _cluster_from(
                scope=scope,
                element_id=element_id,
                element_label=_element_label(scope, element_id, topology),
                # O trecho suspeito vale em todo escopo agregador, não só na
                # PON: em produção o evento mais grave fechou em OLT com 2% da
                # OLT afetada — mandar o técnico "olhar a OLT" seria mandá-lo
                # pro lugar errado. O trecho é o que transforma escopo em ação.
                segment=(
                    ""
                    if scope == SCOPE_CTO
                    else _segment_label(
                        sorted({d.cto_id for d in members if d.cto_id}), drops_by_cto, topology
                    )
                ),
                confidence=_confidence(len(members), denominator),
                members=members,
                fraction=_fraction(len(members), denominator),
            )
        )
        used_logins.update(d.login_id for d in members)
        used_evidence.update(evidence)
        if scope in (SCOPE_POP, SCOPE_OLT):
            # As unidades de evidência do escopo alto também consomem as CTOs que
            # as compõem, senão a mesma caixa reapareceria como massiva de CTO.
            for cto in drops_by_cto:
                if not _under(scope, element_id, cto, topology):
                    continue
                used_evidence.update({cto, f"CTO:{cto}"})
                if cto_pon.get(cto):
                    used_evidence.add(f"PON:{cto_pon[cto]}")
    return clusters


def _under(scope: str, element_id: str, cto_id: str, topology: _ResolvedTopology) -> bool:
    if scope == SCOPE_OLT:
        return topology.transmitter_of(cto_id) == element_id
    return topology.pop_of(cto_id) == element_id


def _members_for(
    scope: str,
    element_id: str,
    drops: Sequence[DropInput],
    drops_by_cto: Mapping[str, list[DropInput]],
    cto_pon: Mapping[str, str],
    topology: _ResolvedTopology,
) -> list[DropInput]:
    """Quedas que entram no cluster do elemento — inclusive as de CTOs pequenas."""
    if scope == SCOPE_CTO:
        return list(drops_by_cto.get(element_id, []))
    if scope == SCOPE_PON:
        ctos = {cto for cto, pon in cto_pon.items() if pon == element_id}
        return [d for d in drops if d.pon_id == element_id or d.cto_id in ctos]
    if scope == SCOPE_OLT:
        return [
            d
            for d in drops
            if d.transmitter_id == element_id or topology.transmitter_of(d.cto_id) == element_id
        ]
    return [d for d in drops if d.pop_id == element_id or topology.pop_of(d.cto_id) == element_id]


def _denominator_for(
    scope: str,
    element_id: str,
    cto_pon: Mapping[str, str],
    topology: _ResolvedTopology,
) -> int:
    if scope == SCOPE_CTO:
        return topology.active_logins(element_id)
    if scope == SCOPE_PON:
        # Não existe contagem de logins ativos por porta PON — o melhor
        # denominador disponível é o das caixas filiadas a ela. Superestima
        # (a caixa pode ser alimentada por outra porta também), e superestimar
        # puxa a fração pra baixo: erra pro lado conservador da confiança.
        ctos = [cto for cto, pon in cto_pon.items() if pon == element_id]
        return sum(topology.active_logins(cto) for cto in ctos)
    return sum(topology.active_logins(cto) for cto in topology.ctos_under(scope, element_id))


def _fraction(affected: int, denominator: int) -> float:
    return (affected / denominator) if denominator else 0.0


def _confidence(affected: int, denominator: int) -> str:
    """ALTA exige fração alta E denominador que sustente a afirmação (§5.5).

    Sem o piso de denominador, uma caixa de 1 login daria 100% e ALTA — e 511 das
    852 CTOs com login ativo têm no máximo 2 logins. Fração sobre denominador
    pequeno não vira confiança alta.
    """
    if denominator < _MIN_DENOMINATOR_FOR_HIGH_CONFIDENCE:
        return CONFIDENCE_MEDIUM
    fraction = _fraction(affected, denominator)
    return CONFIDENCE_HIGH if fraction >= _HIGH_CONFIDENCE_FRACTION else CONFIDENCE_MEDIUM


def _cluster_from(
    *,
    scope: str,
    element_id: str,
    element_label: str,
    segment: str,
    confidence: str,
    members: Sequence[DropInput],
    fraction: float,
) -> OutageCluster:
    ordered = sorted(members, key=lambda d: (d.dropped_at, d.login_id))
    return OutageCluster(
        scope=scope,
        started_at=min(d.dropped_at for d in ordered),
        element_id=element_id,
        element_label=element_label,
        suspected_segment_label=segment,
        confidence=confidence,
        login_ids=tuple(d.login_id for d in ordered),
        affected_count=len(ordered),
        mrr_at_risk=sum((d.monthly_amount for d in ordered), Decimal("0")),
        affected_fraction=fraction,
    )


def _element_label(scope: str, element_id: str, topology: _ResolvedTopology) -> str:
    if scope == SCOPE_CTO:
        return topology.cto_label(element_id)
    if scope == SCOPE_GEO:
        return "Cluster geográfico"
    return f"{scope} {element_id}"


# ---------------------------------------------------------------------------
# Trecho suspeito (§5.4)
# ---------------------------------------------------------------------------


def _segment_label(
    cto_ids: Sequence[str],
    drops_by_cto: Mapping[str, list[DropInput]],
    topology: _ResolvedTopology,
) -> str:
    """Rotula o trecho entre a CTO mais próxima do POP e as demais.

    Só faz sentido com ≥2 CTOs integralmente fora: é a assinatura de rompimento a
    montante das caixas. Nunca nomeia cabo — §2.3.
    """
    fully_out = sorted(
        cto
        for cto in cto_ids
        if topology.active_logins(cto) > 0
        and len(drops_by_cto.get(cto, [])) >= topology.active_logins(cto)
    )
    if len(fully_out) < 2:
        return ""
    upstream = _closest_to_pop(fully_out, topology)
    others = [cto for cto in fully_out if cto != upstream]
    # Em escopo OLT/POP a lista de caixas fora pode ter dezenas de nomes (24 no
    # evento real das 19:46). O rótulo nomeia as primeiras e conta o resto — a
    # lista completa está em `login_ids`, o rótulo é pra ser lido de relance.
    named = ", ".join(topology.cto_label(cto) for cto in others[:_MAX_NAMED_SEGMENT_CTOS])
    remaining = len(others) - _MAX_NAMED_SEGMENT_CTOS
    if remaining > 0:
        suffix = "caixa" if remaining == 1 else "caixas"
        named = f"{named} (+{remaining} {suffix})"
    return f"trecho {topology.cto_label(upstream)} → {named}"


def _closest_to_pop(cto_ids: Sequence[str], topology: _ResolvedTopology) -> str:
    """CTO mais próxima do POP; sem coordenada de POP, a primeira em ordem estável.

    Sem a posição do POP não dá pra saber quem está a montante, então o rótulo
    nomeia o trecho sem afirmar o sentido da falha.
    """
    pop_id = _first_non_empty(topology.pop_of(cto) for cto in cto_ids)
    pop_point = topology.pop_coordinates.get(pop_id) if pop_id else None
    if pop_point is None:
        return sorted(cto_ids)[0]
    best: tuple[float, str] | None = None
    for cto in sorted(cto_ids):
        point = topology.cto_coordinates.get(cto)
        if point is None:
            continue
        distance = _haversine_meters(pop_point, point)
        if best is None or distance < best[0]:
            best = (distance, cto)
    return best[1] if best else sorted(cto_ids)[0]


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


# ---------------------------------------------------------------------------
# Cluster geográfico (§5.3, escopo GEO)
# ---------------------------------------------------------------------------


def _geo_clusters(
    drops: Sequence[DropInput],
    topology: _ResolvedTopology,
    *,
    min_clients: int,
    geo_radius_meters: float,
) -> list[OutageCluster]:
    located = [d for d in drops if d.latitude is not None and d.longitude is not None]
    if len(located) < min_clients:
        return []

    # Ligação simples (union-find): o rompimento entre caixas espalha os clientes
    # ao longo de uma rua, então exigir que todos os pares caibam no raio perderia
    # justamente o caso de interesse.
    parent = list(range(len(located)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(located)):
        for j in range(i + 1, len(located)):
            a, b = located[i], located[j]
            if _haversine_meters(_point(a), _point(b)) <= geo_radius_meters:
                parent[find(i)] = find(j)

    groups: dict[int, list[DropInput]] = defaultdict(list)
    for index, drop in enumerate(located):
        groups[find(index)].append(drop)

    clusters: list[OutageCluster] = []
    for members in groups.values():
        if len(members) < min_clients:
            continue
        by_cto = _by_cto(members)
        clusters.append(
            _cluster_from(
                scope=SCOPE_GEO,
                element_id="",
                element_label=_element_label(SCOPE_GEO, "", topology),
                segment=_segment_label(sorted(by_cto), by_cto, topology),
                confidence=CONFIDENCE_LOW,
                members=members,
                fraction=_geo_fraction(members, topology),
            )
        )
    return clusters


def _point(drop: DropInput) -> tuple[float, float]:
    return (float(drop.latitude or 0.0), float(drop.longitude or 0.0))


def _by_cto(drops: Sequence[DropInput]) -> dict[str, list[DropInput]]:
    grouped: dict[str, list[DropInput]] = defaultdict(list)
    for drop in drops:
        if drop.cto_id:
            grouped[drop.cto_id].append(drop)
    return grouped


def _geo_fraction(drops: Sequence[DropInput], topology: _ResolvedTopology) -> float:
    """No GEO não há elemento em escopo — o denominador é o das CTOs envolvidas."""
    ctos = {d.cto_id for d in drops if d.cto_id}
    return _fraction(len(drops), sum(topology.active_logins(cto) for cto in ctos))


def _haversine_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(h))
