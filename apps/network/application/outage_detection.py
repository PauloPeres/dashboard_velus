"""Orquestra o detector de massivas e reconcilia o resultado com o que já existe (#145/#147).

O detector (`apps.network.domain.outage`) é **sem estado**: recebe as quedas
abertas, devolve os clusters de agora. Roda a cada 3 min, junto com o poll. Como
os clientes de uma mesma massiva não caem todos no mesmo segundo, a mesma
ocorrência é redetectada rodada após rodada, cada vez com mais gente — e às
vezes com o escopo mais alto (a caixa vira PON, a PON vira OLT). Este módulo é
quem transforma essa sequência de fotos numa massiva que **cresce** em vez de
virar uma massiva nova a cada poll.

Regra de identidade — por que é a sobreposição de clientes
----------------------------------------------------------
Um cluster é a **mesma** massiva de um `OutageEvent` ainda aberto quando os dois
compartilham ao menos uma queda (`ConnectionDropEvent`). Não é `(escopo,
elemento)`, e a escolha é deliberada:

- **o escopo muda dentro da mesma ocorrência.** O evento real de 2026-09-08
  começou com uma caixa fora e terminou em escopo OLT com 43 clientes. Casar por
  `(escopo, elemento)` criaria uma massiva de CTO, depois uma de PON, depois uma
  de OLT — três registros para um rompimento;
- **a queda é a identidade natural.** Um cliente está fora uma vez só; enquanto
  ele estiver fora, o evento que o contém é aquela ocorrência. Quando ele volta,
  a queda fecha e some do detector, então a sobreposição não sobrevive ao fim da
  massiva: não há como colar duas ocorrências separadas no tempo;
- **a proximidade temporal sai de graça.** Não precisa de janela arbitrária:
  massivas encerradas não recebem clientes novos (`ended_at` não nulo sai da
  busca) e uma queda já ligada a uma massiva encerrada nem entra no detector,
  senão o resto que não voltou ressuscitaria o evento a cada rodada.

Quando um cluster se sobrepõe a **mais de uma** massiva aberta, elas são a mesma
ocorrência vista em pedaços (duas caixas que viraram um rompimento só). A mais
antiga absorve as outras, que são apagadas — nunca existiram como evento
separado, e deixá-las "encerradas" inventaria massivas que ninguém viveu.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog

from apps.network.domain.outage import (
    DropInput,
    OutageCluster,
    TopologyInput,
    detect_outages,
)
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    NetworkElement,
    OutageAffectedLogin,
    OutageEvent,
)

_logger = structlog.get_logger(__name__)

# Fração de retorno que encerra a massiva sozinha (§6).
_CLOSE_AT_RESTORED_FRACTION = 0.90

# Depois disto sem o detector ver a massiva, o que sobrou fora não é mais
# massiva: é cliente com problema individual, e a tela é de tempo real. Sem este
# corte uma massiva com um cliente que nunca volta fica aberta pra sempre.
_STALE_HOURS = 24


@dataclass(frozen=True)
class ReconciliationResult:
    detected: int = 0
    opened: int = 0
    grown: int = 0
    merged: int = 0
    closed: int = 0


def reconcile_outages(organization: Any, *, now: datetime) -> ReconciliationResult:
    """Detecta massivas nas quedas abertas e concilia com as já registradas.

    Espera a organização já setada no contexto (o poll faz isso). `now` entra
    por parâmetro pra que o teste possa fixar o relógio.
    """
    drops, drops_by_login = _open_drops()
    clusters = (
        detect_outages(drops, _build_topology()) if drops else []
    )

    opened = grown = merged = 0
    for cluster in clusters:
        result = _apply_cluster(organization, cluster, drops_by_login, now=now)
        opened += result[0]
        grown += result[1]
        merged += result[2]

    closed = _refresh_open_outages(now=now)
    return ReconciliationResult(
        detected=len(clusters),
        opened=opened,
        grown=grown,
        merged=merged,
        closed=closed,
    )


# ---------------------------------------------------------------------------
# Entrada do detector
# ---------------------------------------------------------------------------


def _open_drops() -> tuple[list[DropInput], dict[str, ConnectionDropEvent]]:
    """Quedas ainda abertas, exceto as que já pertencem a uma massiva encerrada.

    A exclusão é o que impede a ressurreição: numa massiva de 50 clientes
    encerrada com 90% de retorno, os 5 que continuam fora seriam redetectados
    como massiva nova a cada 3 min.
    """
    encerradas = OutageAffectedLogin.objects.filter(
        outage__ended_at__isnull=False, drop_event__isnull=False
    ).values_list("drop_event_id", flat=True)

    eventos = list(
        ConnectionDropEvent.objects
        .filter(restored_at__isnull=True)
        .exclude(pk__in=list(encerradas))
    )

    drops: list[DropInput] = []
    by_login: dict[str, ConnectionDropEvent] = {}
    for evento in eventos:
        # O detector é indexado por login; a chave precisa ser única e estável,
        # e o `login` do RADIUS pode vir vazio — o pk da queda sempre serve.
        key = str(evento.pk)
        by_login[key] = evento
        drops.append(
            DropInput(
                login_id=key,
                dropped_at=evento.dropped_at,
                cto_id=evento.cto_external_id,
                cto_port=evento.cto_port,
                transmitter_id=evento.transmitter_external_id,
                pon_id=evento.pon_external_id,
                latitude=evento.latitude,
                longitude=evento.longitude,
                reason=evento.reason,
                monthly_amount=evento.monthly_amount or Decimal("0"),
            )
        )
    return drops, by_login


def _build_topology() -> TopologyInput:
    """Denominadores e hierarquia — logins de `Connection`, planta de `NetworkElement`."""
    return TopologyInput(
        active_logins_per_cto=active_logins_per_cto(),
        active_logins_per_pon=active_logins_per_pon(),
        cto_to_transmitter=_cto_to_transmitter(),
        cto_to_pop=_cto_to_pop(),
        cto_coordinates=_cto_coordinates(),
        cto_names=_cto_names(),
        pop_coordinates=_pop_coordinates(),
    )


def active_logins_per_cto() -> dict[str, int]:
    """Quantos logins de contrato ATIVO cada caixa tem.

    Pública porque o denominador não é só do detector: a vizinhança da massiva
    (R6) precisa exatamente do mesmo número, e duas contagens que deveriam ser
    iguais divergem no dia em que uma das duas for corrigida.

    O denominador é de contrato ativo, não de login existente: contrato
    cancelado deixa login para trás no ERP e inflaria a caixa, derrubando a
    fração de todo mundo. Contrato desconhecido (ainda não sincronizado) conta —
    perder o cliente do denominador erraria pro lado de gritar massiva.
    """
    from apps.customers.infrastructure.models import Contract

    inativos = set(
        Contract.objects
        .exclude(status=Contract.Status.ACTIVE)
        .values_list("external_id", flat=True)
    )
    counts: dict[str, int] = defaultdict(int)
    rows = (
        Connection.objects
        .exclude(cto_external_id="")
        .values_list("cto_external_id", "contract_external_id")
    )
    for cto_id, contract_id in rows:
        if contract_id and contract_id in inativos:
            continue
        counts[cto_id] += 1
    return dict(counts)


def active_logins_per_pon() -> dict[str, int]:
    """Quantos logins de contrato ATIVO cada porta PON tem.

    A PON é propriedade do login (§2.5c), então ela se conta direto da
    `Connection` — não pela caixa. Era isso que faltava: o denominador da PON
    vinha da soma das caixas *que qualificaram no degrau de CTO*, enquanto o
    numerador pegava todo login da porta, e a fração passava de 100% (225% na
    PON 364, em produção).

    *Medido em 2026-09-18:* 3.275 das 8.294 conexões têm `pon_external_id`. A
    porta que não aparece aqui devolve 0, e o cálculo cai no denominador antigo
    em vez de inventar cobertura que não existe.
    """
    from apps.customers.infrastructure.models import Contract

    inativos = set(
        Contract.objects
        .exclude(status=Contract.Status.ACTIVE)
        .values_list("external_id", flat=True)
    )
    counts: dict[str, int] = defaultdict(int)
    rows = (
        Connection.objects
        .exclude(pon_external_id="")
        .values_list("pon_external_id", "contract_external_id")
    )
    for pon_id, contract_id in rows:
        if contract_id and contract_id in inativos:
            continue
        counts[pon_id] += 1
    return dict(counts)


def _cto_to_transmitter() -> dict[str, str]:
    return {
        external_id: parent
        for external_id, parent in NetworkElement.objects.filter(
            kind=NetworkElement.Kind.CTO, parent_kind=NetworkElement.Kind.OLT
        ).values_list("external_id", "parent_external_id")
        if parent
    }


def _cto_to_pop() -> dict[str, str]:
    """CTO → POP, subindo pela OLT: a caixa não guarda POP, o transmissor guarda."""
    olt_to_pop = {
        external_id: parent
        for external_id, parent in NetworkElement.objects.filter(
            kind=NetworkElement.Kind.OLT, parent_kind=NetworkElement.Kind.POP
        ).values_list("external_id", "parent_external_id")
        if parent
    }
    return {
        cto: olt_to_pop[olt]
        for cto, olt in _cto_to_transmitter().items()
        if olt in olt_to_pop
    }


def _coordinates(kind: str) -> dict[str, tuple[float, float]]:
    return {
        external_id: (lat, lon)
        for external_id, lat, lon in NetworkElement.objects.filter(
            kind=kind, latitude__isnull=False, longitude__isnull=False
        ).values_list("external_id", "latitude", "longitude")
    }


def _cto_coordinates() -> dict[str, tuple[float, float]]:
    return _coordinates(NetworkElement.Kind.CTO)


def _pop_coordinates() -> dict[str, tuple[float, float]]:
    return _coordinates(NetworkElement.Kind.POP)


def _cto_names() -> dict[str, str]:
    return {
        external_id: name
        for external_id, name in NetworkElement.objects.filter(
            kind=NetworkElement.Kind.CTO
        ).values_list("external_id", "name")
        if name
    }


# ---------------------------------------------------------------------------
# Reconciliação
# ---------------------------------------------------------------------------


def _apply_cluster(
    organization: Any,
    cluster: OutageCluster,
    drops_by_login: dict[str, ConnectionDropEvent],
    *,
    now: datetime,
) -> tuple[int, int, int]:
    """Cria, faz crescer ou funde a massiva do cluster. Devolve (aberta, cresceu, fundida)."""
    eventos = [drops_by_login[login] for login in cluster.login_ids if login in drops_by_login]
    if not eventos:
        return (0, 0, 0)

    candidatas = _open_outages_sharing(eventos)
    merged = 0
    if candidatas:
        outage = candidatas[0]
        for absorvida in candidatas[1:]:
            _absorb(outage, absorvida)
            merged += 1
        opened, grown = 0, 1
    else:
        outage = OutageEvent(organization=organization, started_at=cluster.started_at)
        opened, grown = 1, 0

    _link_drops(organization, outage, eventos, cluster=cluster, now=now)
    return (opened, grown, merged)


def _open_outages_sharing(eventos: list[ConnectionDropEvent]) -> list[OutageEvent]:
    """Massivas abertas que já contêm alguma destas quedas, a mais antiga primeiro."""
    ids = set(
        OutageAffectedLogin.objects
        .filter(drop_event__in=eventos, outage__ended_at__isnull=True)
        .values_list("outage_id", flat=True)
    )
    return list(OutageEvent.objects.filter(pk__in=ids).order_by("started_at", "pk"))


def _absorb(survivor: OutageEvent, absorvida: OutageEvent) -> None:
    """Funde duas massivas que se revelaram a mesma ocorrência.

    A absorvida é apagada, não encerrada: ela nunca foi uma massiva separada —
    foi o mesmo rompimento visto por um pedaço só antes de as outras caixas
    caírem. Deixá-la como "encerrada" poria no histórico um evento que não
    aconteceu.
    """
    ja_ligadas = set(
        OutageAffectedLogin.objects
        .filter(outage=survivor)
        .values_list("drop_event_id", flat=True)
    )
    for link in OutageAffectedLogin.objects.filter(outage=absorvida):
        if link.drop_event_id in ja_ligadas:
            link.delete()
            continue
        link.outage = survivor
        link.save(update_fields=["outage", "updated_at"])
    _logger.info(
        "outage_merged", survivor=survivor.pk, absorbed=absorvida.pk
    )
    absorvida.delete()


def _link_drops(
    organization: Any,
    outage: OutageEvent,
    eventos: list[ConnectionDropEvent],
    *,
    cluster: OutageCluster,
    now: datetime,
) -> None:
    """Grava o escopo da detecção mais recente e anexa as quedas que faltavam."""
    outage.scope = cluster.scope
    outage.element_external_id = cluster.element_id
    outage.element_label = cluster.element_label
    outage.suspected_segment_label = cluster.suspected_segment_label
    outage.confidence = cluster.confidence
    outage.suspected_element = _element_for(cluster)
    # A fração guardada é o PICO. Conforme os clientes voltam, o cluster vivo
    # encolhe e a fração de agora cairia — mas a pergunta que a fração responde
    # ("quanto do elemento caiu") é sobre o pico, não sobre o resto.
    outage.affected_fraction = max(outage.affected_fraction or 0.0, cluster.affected_fraction)
    outage.last_detected_at = now
    if cluster.started_at < outage.started_at:
        outage.started_at = cluster.started_at
    outage.save()

    existentes = set(
        OutageAffectedLogin.objects
        .filter(outage=outage)
        .values_list("drop_event_id", flat=True)
    )
    novos = [
        OutageAffectedLogin(
            organization=organization,
            outage=outage,
            drop_event=evento,
            login=evento.login,
            dropped_at=evento.dropped_at,
            restored_at=evento.restored_at,
            monthly_amount=evento.monthly_amount,
        )
        for evento in eventos
        if evento.pk not in existentes
    ]
    if novos:
        OutageAffectedLogin.objects.bulk_create(novos)

    _recount(outage)


def _element_for(cluster: OutageCluster) -> NetworkElement | None:
    """Elemento cadastrado do escopo, se existir — o rótulo já veio pronto do domínio."""
    if not cluster.element_id or cluster.scope not in NetworkElement.Kind.values:
        return None
    return NetworkElement.objects.filter(
        kind=cluster.scope, external_id=cluster.element_id
    ).first()


# ---------------------------------------------------------------------------
# Retorno ao ar (#147)
# ---------------------------------------------------------------------------


def _refresh_open_outages(*, now: datetime) -> int:
    """Propaga o retorno por cliente, atualiza contadores e encerra as que voltaram."""
    encerradas = 0
    for outage in OutageEvent.objects.filter(ended_at__isnull=True):
        _propagate_restorations(outage)
        _recount(outage)
        if _close_if_restored(outage) or _close_if_stale(outage, now=now):
            encerradas += 1
    return encerradas


def _propagate_restorations(outage: OutageEvent) -> None:
    """O poll fecha a queda; aqui o retorno vira progresso da massiva.

    O instante fica gravado no vínculo (e não só na queda) porque a queda é
    estado de trabalho: o agregado da massiva tem que sobreviver a ela.
    """
    links = OutageAffectedLogin.objects.filter(
        outage=outage, restored_at__isnull=True, drop_event__isnull=False
    ).select_related("drop_event")
    for link in links:
        if link.drop_event.restored_at is not None:
            link.restored_at = link.drop_event.restored_at
            link.save(update_fields=["restored_at", "updated_at"])


def _recount(outage: OutageEvent) -> None:
    afetados = 0
    restaurados = 0
    mrr = Decimal("0")
    for restored_at, monthly_amount in OutageAffectedLogin.objects.filter(
        outage=outage
    ).values_list("restored_at", "monthly_amount"):
        afetados += 1
        if restored_at is not None:
            restaurados += 1
        mrr += monthly_amount or Decimal("0")
    outage.affected_count = afetados
    outage.restored_count = restaurados
    outage.mrr_at_risk = mrr
    outage.save(
        update_fields=["affected_count", "restored_count", "mrr_at_risk", "updated_at"]
    )


def _close_if_restored(outage: OutageEvent) -> bool:
    """Encerra quando ≥90% voltou (§6), datando pelo último retorno de verdade."""
    if not outage.affected_count:
        return False
    if outage.restored_count / outage.affected_count < _CLOSE_AT_RESTORED_FRACTION:
        return False
    ultimo = (
        OutageAffectedLogin.objects
        .filter(outage=outage, restored_at__isnull=False)
        .order_by("-restored_at")
        .values_list("restored_at", flat=True)
        .first()
    )
    outage.ended_at = ultimo or outage.last_detected_at
    outage.save(update_fields=["ended_at", "updated_at"])
    _logger.info(
        "outage_closed_by_restoration",
        outage=outage.pk,
        affected=outage.affected_count,
        restored=outage.restored_count,
    )
    return True


def _close_if_stale(outage: OutageEvent, *, now: datetime) -> bool:
    if now - outage.last_detected_at < timedelta(hours=_STALE_HOURS):
        return False
    outage.ended_at = outage.last_detected_at
    outage.save(update_fields=["ended_at", "updated_at"])
    _logger.warning(
        "outage_closed_stale",
        outage=outage.pk,
        affected=outage.affected_count,
        restored=outage.restored_count,
    )
    return True
