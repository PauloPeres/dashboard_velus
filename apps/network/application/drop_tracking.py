"""Diff de estado de conexão → eventos de queda (#143).

O coração é `diff_drop_events`: **função pura**, sem Django, sem ORM, sem
relógio próprio. Recebe as quedas hoje abertas e as linhas recém-sincronizadas,
devolve o que abrir e o que fechar. Quem persiste é a camada de infraestrutura;
quem decide é isto aqui — assim a regra pode ser testada com um dicionário e uma
data, e não com um banco.

**Partida a frio.** A primeira observação de um login estabelece a linha de
base; ela não gera queda. O IXC tem hoje 237 logins ativos offline e boa parte
deles caiu há semanas — `ultima_conexao_final` de agosto, de junho. Sem esta
regra, a primeira rodada do poll leria tudo isso como queda recente e a
ferramenta estrearia inventando massivas que nunca existiram. Só abre evento:

1. a transição observada online → offline (`previous_status` diz que estava no
   ar da última vez que olhamos), ou
2. a queda cujo `last_disconnection_at` é **posterior** ao `baseline_at` — o
   instante em que passamos a observar aquela organização.

Sem `baseline_at`, vale só o critério (1), que é o mais conservador.

Regra, direto do plano (§5.6 e §6):

- só **OFFLINE** abre queda. BLOCKED é corte comercial e UNKNOWN é ausência de
  sessão registrada no ERP (o `online` "SS"/"" do IXC); nenhum dos dois é falha
  de rede e contá-los afogaria a massiva real em ruído;
- ONLINE fecha a queda aberta;
- OFFLINE com uma desconexão **mais recente** que a queda aberta significa que o
  cliente voltou e caiu de novo entre dois polls: fecha a antiga e abre a nova,
  senão duas quedas distintas viram uma só, longa e sem hora certa;
- BLOCKED/UNKNOWN sobre uma queda aberta **fecha** a queda: o login saiu do
  universo de "fora do ar por falha", e deixá-la aberta a manteria contando pra
  sempre no painel de "clientes fora".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

# Status que representam "fora do ar por falha". Só eles abrem queda.
DROP_STATUSES: frozenset[str] = frozenset({"OFFLINE"})
# Status que representam "de volta ao ar".
RESTORED_STATUSES: frozenset[str] = frozenset({"ONLINE"})


@dataclass(frozen=True)
class ConnectionState:
    """Estado corrente de um login, como o sync acabou de trazer.

    `key` é opaco — a camada de persistência escolhe o que usar (o pk da
    Connection, na prática). A função pura só compara chaves.
    """

    key: Any
    status: str
    login: str = ""

    # Estado da última observação (o que está gravado em Connection antes deste
    # sync). "" significa que nunca observamos — é a linha de base, não queda.
    previous_status: str = ""

    last_connection_at: datetime | None = None
    last_disconnection_at: datetime | None = None
    disconnect_reason: str = ""

    # Snapshot da topologia — copiado pro evento no instante da queda porque o
    # cliente pode mudar de CTO depois e a massiva de ontem tem que continuar
    # contando o que era ontem.
    cto_external_id: str = ""
    cto_port: str = ""
    pon_external_id: str = ""
    transmitter_external_id: str = ""
    latitude: float | None = None
    longitude: float | None = None
    monthly_amount: Decimal | None = None


@dataclass(frozen=True)
class OpenDrop:
    """Uma queda já registrada e ainda sem retorno."""

    key: Any
    dropped_at: datetime
    event_id: Any = None


@dataclass(frozen=True)
class DropToOpen:
    """Queda a registrar, já com o snapshot da topologia."""

    key: Any
    login: str
    dropped_at: datetime
    reason: str = ""
    cto_external_id: str = ""
    cto_port: str = ""
    pon_external_id: str = ""
    transmitter_external_id: str = ""
    latitude: float | None = None
    longitude: float | None = None
    monthly_amount: Decimal | None = None


@dataclass(frozen=True)
class DropToClose:
    """Queda a encerrar."""

    key: Any
    event_id: Any
    restored_at: datetime


@dataclass(frozen=True)
class DropDiff:
    to_open: list[DropToOpen] = field(default_factory=list)
    to_close: list[DropToClose] = field(default_factory=list)


def diff_drop_events(
    *,
    open_drops: list[OpenDrop],
    current: list[ConnectionState],
    now: datetime,
    baseline_at: datetime | None = None,
) -> DropDiff:
    """Compara quedas abertas com o estado novo e devolve o que abrir e fechar.

    `now` e `baseline_at` entram por parâmetro (e não de `timezone.now()`) pra
    função ficar pura e o teste poder fixar os instantes.
    """
    by_key: dict[Any, OpenDrop] = {drop.key: drop for drop in open_drops}
    diff = DropDiff()

    for state in current:
        open_drop = by_key.get(state.key)
        status = (state.status or "").upper()

        if status in RESTORED_STATUSES:
            if open_drop is not None:
                diff.to_close.append(
                    _close(open_drop, _restored_moment(state, open_drop, now))
                )
            continue

        if status not in DROP_STATUSES:
            # BLOCKED/UNKNOWN: não abre queda; se havia uma aberta, encerra.
            if open_drop is not None:
                diff.to_close.append(_close(open_drop, now))
            continue

        dropped_at = state.last_disconnection_at or now

        if open_drop is None:
            if _is_baseline(state, dropped_at, baseline_at):
                continue
            diff.to_open.append(_open(state, dropped_at))
            continue

        if dropped_at > open_drop.dropped_at:
            # Voltou e caiu de novo entre dois polls: a queda anterior terminou
            # em algum momento antes desta — o melhor limite disponível é o
            # início da sessão que acabou de cair.
            restored_at = state.last_connection_at or dropped_at
            if restored_at < open_drop.dropped_at:
                restored_at = dropped_at
            diff.to_close.append(_close(open_drop, restored_at))
            diff.to_open.append(_open(state, dropped_at))

    return diff


def _is_baseline(
    state: ConnectionState,
    dropped_at: datetime,
    baseline_at: datetime | None,
) -> bool:
    """Esta queda é só a foto inicial do login, e não um evento novo?

    Ver "Partida a frio" no topo do módulo: 237 logins offline hoje, muitos
    caídos há semanas. Nenhum deles é notícia.
    """
    # Transição observada online → offline: é queda de verdade.
    if (state.previous_status or "").upper() in RESTORED_STATUSES:
        return False
    # Sem `last_disconnection_at` não há prova de que a queda é de agora: o
    # `dropped_at` aí é só o horário do poll, que passaria por qualquer baseline.
    if state.last_disconnection_at is None:
        return True
    # Caiu depois de começarmos a observar: também é queda de verdade.
    return not (baseline_at is not None and dropped_at > baseline_at)


def _restored_moment(
    state: ConnectionState, open_drop: OpenDrop, now: datetime
) -> datetime:
    """Melhor estimativa de quando o login voltou.

    `last_connection_at` é o início da sessão atual — o instante exato do
    retorno, desde que seja posterior à queda. Quando não bate (ou falta),
    sobra o horário do poll, que é um limite superior honesto.
    """
    inicio = state.last_connection_at
    if inicio is not None and inicio > open_drop.dropped_at:
        return inicio
    return now


def _open(state: ConnectionState, dropped_at: datetime) -> DropToOpen:
    return DropToOpen(
        key=state.key,
        login=state.login,
        dropped_at=dropped_at,
        reason=state.disconnect_reason,
        cto_external_id=state.cto_external_id,
        cto_port=state.cto_port,
        pon_external_id=state.pon_external_id,
        transmitter_external_id=state.transmitter_external_id,
        latitude=state.latitude,
        longitude=state.longitude,
        monthly_amount=state.monthly_amount,
    )


def _close(open_drop: OpenDrop, restored_at: datetime) -> DropToClose:
    return DropToClose(
        key=open_drop.key,
        event_id=open_drop.event_id,
        restored_at=restored_at,
    )
