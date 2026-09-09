"""Poll de status de conexão a cada 3 min (#144).

O sync completo de rede roda de 6 em 6 horas e é inútil pra triagem de queda ao
vivo. Este poll faz uma coisa só e barata: lê a lista de logins ativos que estão
fora do ar (~240 linhas numa chamada), atualiza `Connection`, abre e fecha
`ConnectionDropEvent` pelo diff puro de `drop_tracking` e chama o detector de
massivas no fim.

Duas decisões que carregam o módulo:

**Quem voltou some da lista.** O poll pergunta só por quem está fora, então o
retorno não vem como um registro "online" — vem como ausência. Por isso as
quedas abertas cujo login não apareceu na leitura entram no diff como ONLINE
sintético. A leitura não distingue "voltou" de "foi bloqueado no ERP"; nos dois
casos a queda deixa de ser falha de rede em curso e tem que fechar, e o sync de
6h corrige o status real depois.

**Uma leitura que falhou não é notícia de que ninguém caiu.** O host do IXC é
dual-stack sem rota IPv6 no cluster e a API às vezes devolve uma página HTML de
erro em vez de JSON. Se a leitura falhar (inclusive no meio da paginação), a
rodada inteira é descartada: nada é aberto e — sobretudo — nada é fechado, pois
"não veio na lista" só significa "voltou" quando a lista veio inteira.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from django.utils import timezone

from apps.integrations.shared.enums import SourceType
from apps.network.application.drop_tracking import (
    ConnectionState,
    OpenDrop,
    diff_drop_events,
)
from apps.network.application.outage_detection import reconcile_outages
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    ConnectionPollState,
)
from apps.network.infrastructure.repositories import ConnectionRepository

_logger = structlog.get_logger(__name__)

# Status de contrato que tiram o login do universo de "fora do ar por falha"
# (§5.6): corte comercial não é queda. Entram no diff como BLOCKED, que não abre
# queda e encerra a que estiver aberta.
_NON_FAILURE_CONTRACT_STATUSES = frozenset({"CANCELED", "BLOCKED"})


@dataclass(frozen=True)
class PollResult:
    read: int = 0
    opened: int = 0
    closed: int = 0
    outages_detected: int = 0
    outages_opened: int = 0
    outages_closed: int = 0
    failed: bool = False
    # Quedas encerradas nesta rodada. É a lista que dispara a medição de sinal
    # óptico no retorno (#148): o poll não mede nada — ele diz quem voltou, e a
    # medição cara acontece uma vez, sob esse evento, na camada de tarefas.
    restored_event_ids: tuple[int, ...] = ()


def run_connection_poll(
    organization: Any,
    source: Any,
    *,
    now: datetime | None = None,
) -> PollResult:
    """Roda uma rodada do poll para uma organização. `source` é um ConnectionSourcePort."""
    now = now or timezone.now()
    source_type: SourceType = source.source_type
    state = _poll_state(organization, now=now)

    try:
        # Materializa dentro do try: a paginação do IXC pode estourar no meio, e
        # meia lista de offline seria interpretada como "metade voltou".
        offline = list(source.list_offline_connections())
    except Exception as exc:
        state.last_poll_at = now
        state.save(update_fields=["last_poll_at", "updated_at"])
        _logger.warning(
            "connection_poll_read_failed",
            organization=getattr(organization, "slug", None),
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return PollResult(failed=True)

    repository = ConnectionRepository(organization)
    states: list[ConnectionState] = []
    vistos: set[int] = set()
    contratos = _contracts_by_external_id()

    for dto in offline:
        connection, _created, previous_status = repository.apply_status_snapshot(
            dto, source_type=source_type
        )
        vistos.add(connection.pk)
        states.append(
            _state_from(
                connection, previous_status=previous_status, contratos=contratos
            )
        )

    open_drops = list(
        ConnectionDropEvent.objects
        .filter(restored_at__isnull=True)
        .values_list("pk", "connection_id", "dropped_at")
    )
    states.extend(_restored_states(open_drops, vistos))

    diff = diff_drop_events(
        open_drops=[
            OpenDrop(key=connection_id, dropped_at=dropped_at, event_id=pk)
            for pk, connection_id, dropped_at in open_drops
        ],
        current=states,
        now=now,
        baseline_at=state.baseline_at,
    )

    opened = _open_events(organization, diff.to_open)
    closed, restored_event_ids = _close_events(diff.to_close)

    outages = reconcile_outages(organization, now=now)

    state.last_poll_at = now
    state.last_success_at = now
    state.save(update_fields=["last_poll_at", "last_success_at", "updated_at"])

    _logger.info(
        "connection_poll_done",
        organization=getattr(organization, "slug", None),
        read=len(offline),
        opened=opened,
        closed=closed,
        outages_detected=outages.detected,
    )
    return PollResult(
        read=len(offline),
        opened=opened,
        closed=closed,
        outages_detected=outages.detected,
        outages_opened=outages.opened,
        outages_closed=outages.closed,
        restored_event_ids=restored_event_ids,
    )


# ---------------------------------------------------------------------------
# Estado observado
# ---------------------------------------------------------------------------


def _poll_state(organization: Any, *, now: datetime) -> ConnectionPollState:
    """Linha de base da organização — criada na primeira rodada (§5.7)."""
    state, _created = ConnectionPollState.objects.get_or_create(
        organization=organization, defaults={"baseline_at": now}
    )
    return state


def _state_from(
    connection: Connection,
    *,
    previous_status: str,
    contratos: dict[str, tuple[str, Decimal]],
) -> ConnectionState:
    contrato = contratos.get(connection.contract_external_id)
    status = connection.status
    if contrato is not None and contrato[0] in _NON_FAILURE_CONTRACT_STATUSES:
        # Contrato cancelado/bloqueado: o login está fora porque foi cortado, não
        # porque a rede caiu. BLOCKED aqui não abre queda e encerra a aberta.
        status = Connection.Status.BLOCKED.value
    return ConnectionState(
        key=connection.pk,
        status=status,
        login=connection.login,
        previous_status=previous_status,
        last_connection_at=connection.last_connection_at,
        last_disconnection_at=connection.last_disconnection_at,
        disconnect_reason=connection.disconnect_reason,
        cto_external_id=connection.cto_external_id,
        cto_port=connection.cto_port,
        pon_external_id=connection.pon_external_id,
        transmitter_external_id=connection.transmitter_external_id,
        latitude=connection.latitude,
        longitude=connection.longitude,
        monthly_amount=contrato[1] if contrato is not None else None,
    )


def _restored_states(
    open_drops: list[tuple[int, int, datetime]], vistos: set[int]
) -> list[ConnectionState]:
    """ONLINE sintético pra quem tinha queda aberta e sumiu da lista de offline.

    Sem `last_connection_at` de propósito: não sabemos a hora exata do retorno
    (só sabemos que já não está fora), e `drop_tracking` usa o instante do poll,
    que é um limite superior honesto.

    O ONLINE fica no diff e **não é gravado** em `Connection.status`: a ausência
    da lista não prova que o login voltou (pode ter sido bloqueado no ERP), e o
    poll não lê o registro dele. Quem responde "está fora agora" é a queda
    aberta; o status corrente continua sendo do sync de 6h. Que o
    `previous_status` fique OFFLINE não custa a próxima queda — o `baseline_at`
    cobre exatamente esse caso.
    """
    ausentes = [conn_id for _pk, conn_id, _dropped in open_drops if conn_id not in vistos]
    if not ausentes:
        return []
    return [
        ConnectionState(
            key=connection.pk,
            status=Connection.Status.ONLINE.value,
            login=connection.login,
            previous_status=connection.status,
        )
        for connection in Connection.objects.filter(pk__in=ausentes)
    ]


def _contracts_by_external_id() -> dict[str, tuple[str, Decimal]]:
    """Status e MRR de cada contrato, numa consulta só.

    Uma consulta por login offline seriam ~240 idas ao banco a cada 3 min pra
    responder duas perguntas de um mapa que cabe em memória.
    """
    from apps.customers.infrastructure.models import Contract

    return {
        external_id: (
            status,
            (amount or Decimal("0"))
            + (addons or Decimal("0"))
            - (discounts or Decimal("0")),
        )
        for external_id, status, amount, addons, discounts in Contract.objects.values_list(
            "external_id",
            "status",
            "monthly_amount",
            "monthly_amount_addons",
            "monthly_amount_discounts",
        )
    }


# ---------------------------------------------------------------------------
# Persistência das quedas
# ---------------------------------------------------------------------------


def _open_events(organization: Any, to_open: list[Any]) -> int:
    if not to_open:
        return 0
    conexoes = {
        c.pk: c
        for c in Connection.objects.filter(pk__in=[d.key for d in to_open])
    }
    eventos = []
    for drop in to_open:
        connection = conexoes.get(drop.key)
        if connection is None:
            continue
        eventos.append(
            ConnectionDropEvent(
                organization=organization,
                connection=connection,
                customer=connection.customer,
                login=drop.login,
                dropped_at=drop.dropped_at,
                reason=drop.reason,
                cto_external_id=drop.cto_external_id,
                cto_port=drop.cto_port,
                pon_external_id=drop.pon_external_id,
                transmitter_external_id=drop.transmitter_external_id,
                latitude=drop.latitude,
                longitude=drop.longitude,
                monthly_amount=drop.monthly_amount,
            )
        )
    # `ignore_conflicts` cobre o unique parcial de queda aberta por conexão: dois
    # polls sobrepostos (ou um retry do Celery) não podem dobrar a massiva.
    ConnectionDropEvent.objects.bulk_create(eventos, ignore_conflicts=True)
    return len(eventos)


def _close_events(to_close: list[Any]) -> tuple[int, tuple[int, ...]]:
    """Fecha as quedas e devolve (quantas, quais).

    Os ids voltam porque o retorno é o gatilho da medição de sinal óptico (#148),
    e só conta o que **esta** rodada fechou: o `update` filtrado por
    `restored_at__isnull=True` garante que dois polls sobrepostos não disparem a
    mesma medição duas vezes.
    """
    fechados = 0
    ids: list[int] = []
    for drop in to_close:
        atualizados = ConnectionDropEvent.objects.filter(
            pk=drop.event_id, restored_at__isnull=True
        ).update(restored_at=drop.restored_at)
        fechados += atualizados
        if atualizados and drop.event_id is not None:
            ids.append(drop.event_id)
    return fechados, tuple(ids)
