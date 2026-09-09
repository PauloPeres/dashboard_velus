"""Sinal óptico por cliente: linha de base, medição no retorno e ação manual (#148).

Para que serve
--------------
Pegar **fusão mal feita depois de um reparo**. O técnico emenda a fibra, o
cliente volta a conectar, o painel de massiva marca "restaurado" — e o enlace
está 5 dB pior do que era. Sem o par antes/depois, ninguém percebe até o cliente
ligar de novo daqui a duas semanas.

A regra de acionamento, que é a decisão de arquitetura
------------------------------------------------------
**O poll barato carrega o trabalho; a medição cara é disparada pelo retorno.**
O poll de status (#144) já lê todos os logins fora do ar numa única listagem e
detecta o retorno por ausência. É esse retorno que dispara a consulta à OLT,
**uma vez por cliente**. Não existe varredura periódica das ONUs afetadas: isso
trocaria algumas dezenas de consultas por milhares, contra equipamento de
produção. Sem massiva aberta e sem retorno, nenhuma chamada sai daqui.

Três freios, todos deliberados:

- **atraso após o retorno** (`measure_delay_seconds`, ~60 s): ONU recém-ligada
  ainda está estabilizando, e medir cedo demais registra um valor que não é o
  do enlace reparado;
- **teto por rodada** (`max_calls_per_round`): a OLT é equipamento de produção;
- **recuo em leitura vazia consecutiva**: uma sequência de medições sem resposta
  é sintoma de OLT ocupada ou de API fora do ar, e insistir é a pior reação
  possível.

O que não se faz aqui
---------------------
Não se interpreta a causa que a OLT reportou. `dying-gasp` sugere que a ONU
perdeu energia — o que apontaria falta de luz, e não fibra rompida — mas essa é
leitura do time, ainda não observada numa massiva real. O código guarda o texto
que veio e cala a boca.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from django.conf import settings
from django.utils import timezone

from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    OutageAffectedLogin,
)

_logger = structlog.get_logger(__name__)

# Atraso entre o retorno do login e a medição. A ONU acabou de subir; medir no
# mesmo segundo pega o transitório, não o enlace reparado.
DEFAULT_MEASURE_DELAY_SECONDS = 60

# Teto rígido de consultas à OLT por rodada. A maior massiva real medida teve 44
# clientes; 25 por rodada cobre o retorno em duas passadas de 3 min sem nunca
# transformar um evento grande numa enxurrada de consultas simultâneas.
DEFAULT_MAX_CALLS_PER_ROUND = 25

# Piora (em dB) a partir da qual a leitura vira notícia. Sugestão calibrada no
# dado real: uma ONU saudável variou de -24,43 a -23,76 em dez dias, menos de
# 0,7 dB. 3 dB é inequívoco. Fica configurável porque é decisão operacional —
# não constante mágica no meio do código.
DEFAULT_DEGRADATION_THRESHOLD_DB = 3.0

# Quantas leituras vazias em sequência encerram a rodada. Vazio é o caso normal
# de uma ONU só (a OLT não a conhece mais); vazio em série é sintoma do outro
# lado, e a resposta certa é parar.
_MAX_CONSECUTIVE_EMPTY = 5

# Quantas quedas abertas buscam linha de base por rodada. É leitura de listagem
# (não toca na OLT), mas é uma chamada por ONU sem leitura corrente utilizável.
_DEFAULT_BASELINE_LIMIT = 50


def measure_delay_seconds() -> int:
    return int(
        getattr(
            settings, "OPTICAL_SIGNAL_MEASURE_DELAY_SECONDS",
            DEFAULT_MEASURE_DELAY_SECONDS,
        )
    )


def max_calls_per_round() -> int:
    return int(
        getattr(
            settings, "OPTICAL_SIGNAL_MAX_CALLS_PER_ROUND",
            DEFAULT_MAX_CALLS_PER_ROUND,
        )
    )


def degradation_threshold_db() -> float:
    return float(
        getattr(
            settings, "OPTICAL_SIGNAL_DEGRADATION_THRESHOLD_DB",
            DEFAULT_DEGRADATION_THRESHOLD_DB,
        )
    )


@dataclass(frozen=True)
class MeasurementResult:
    """Contabilidade de uma rodada de medição ativa.

    `empty` separado de `measured` de propósito: a cobertura é declarada na tela,
    e "medi 12 e 7 não responderam" é uma frase honesta que "medi 12" esconde.
    """

    measured: int = 0
    empty: int = 0
    skipped: int = 0
    stopped_early: bool = False


# ---------------------------------------------------------------------------
# Leitura passiva — linha de base
# ---------------------------------------------------------------------------


def refresh_optical_signals(organization: Any, source: Any) -> int:
    """Atualiza `Connection` com a leitura corrente de todas as ONUs.

    Uma listagem, sem tocar na OLT. Roda 1x/dia porque a coleta que a alimenta é
    diária: chamá-la a cada poll gastaria 4.559 linhas por rodada para reescrever
    exatamente os mesmos valores.

    Ausência entra como ausência: ONU sem potência grava `signal_rx=None` e
    `signal_measured_at=None`, e não zero. São ~30% das ONUs — fingir leitura
    para elas seria inventar 30% da tela.
    """
    por_login: dict[str, Any] = {}
    por_onu: dict[str, Any] = {}
    for dto in source.list_optical_signals():
        if dto.login_external_id:
            por_login[dto.login_external_id] = dto
        por_onu[dto.onu_external_id] = dto

    if not por_login and not por_onu:
        return 0

    atualizados = 0
    conexoes = Connection.objects.filter(organization=organization).only(
        "id", "external_id", "onu_external_id", "signal_rx", "signal_tx",
        "signal_measured_at", "onu_last_drop_cause",
    )
    for connection in conexoes.iterator(chunk_size=500):
        dto = por_login.get(connection.external_id)
        if dto is None and connection.onu_external_id:
            dto = por_onu.get(connection.onu_external_id)
        if dto is None:
            continue
        if _apply_reading(connection, dto, keep_run_state=True):
            atualizados += 1

    _logger.info(
        "optical_signal_refresh_done",
        organization=getattr(organization, "slug", None),
        onus=len(por_onu),
        updated=atualizados,
    )
    return atualizados


def capture_drop_baselines(
    organization: Any, source: Any, *, limit: int | None = None
) -> int:
    """Preenche `signal_rx_before` nas quedas abertas que ainda não têm.

    **A armadilha principal do #148 mora aqui.** A linha de base tem que ser a
    última leitura *válida* anterior à queda:

    1. se a leitura corrente do login tem potência **e** é anterior à queda, ela
       serve — é a varredura diária do IXC, com carimbo de até 24 h;
    2. senão (potência zerada, carimbo nulo, ou leitura posterior à queda),
       procura no histórico a última linha com potência antes da queda;
    3. se nada disso existe, **não grava nada**. Ausência é ausência: uma queda
       sem linha de base aparece na tela como "sem leitura anterior", nunca como
       zero.

    Passo 2 é o que impede o alarme falso em massa: `sinal_rx = 0.00` está em
    1.391 dos 4.554 registros, e tomá-lo por valor mostraria "24 dB de perda"
    para todo cliente que caiu.

    Não toca na OLT — é listagem do ERP.
    """
    teto = limit if limit is not None else _DEFAULT_BASELINE_LIMIT
    pendentes = list(
        ConnectionDropEvent.objects
        .filter(
            organization=organization,
            signal_before_measured_at__isnull=True,
            signal_rx_before__isnull=True,
        )
        .select_related("connection")
        .order_by("-dropped_at")[:teto]
    )

    preenchidos = 0
    for drop in pendentes:
        leitura = _baseline_for(drop, source)
        if leitura is None:
            continue
        drop.signal_rx_before = leitura.signal_rx
        drop.signal_before_measured_at = leitura.measured_at
        drop.save(update_fields=[
            "signal_rx_before", "signal_before_measured_at", "updated_at",
        ])
        preenchidos += 1
    return preenchidos


def _baseline_for(drop: ConnectionDropEvent, source: Any) -> Any:
    connection = drop.connection
    if (
        connection.signal_rx is not None
        and connection.signal_measured_at is not None
        and connection.signal_measured_at <= drop.dropped_at
    ):
        from apps.network.domain.dto import OpticalSignalDTO

        return OpticalSignalDTO(
            onu_external_id=connection.onu_external_id or connection.external_id,
            login_external_id=connection.external_id,
            signal_rx=connection.signal_rx,
            signal_tx=connection.signal_tx,
            measured_at=connection.signal_measured_at,
        )
    if not connection.onu_external_id:
        return None
    return source.last_valid_signal_before(
        onu_external_id=connection.onu_external_id, before=drop.dropped_at
    )


# ---------------------------------------------------------------------------
# Medição ativa — só sob evento
# ---------------------------------------------------------------------------


def measure_after_restore(
    organization: Any,
    source: Any,
    *,
    event_ids: list[int],
    now: datetime | None = None,
) -> MeasurementResult:
    """Mede as ONUs dos logins que **acabaram de voltar**. Uma vez por queda.

    Sem `event_ids`, nenhuma chamada sai — e é essa a garantia que separa esta
    feature de uma varredura de fundo. Quedas já medidas são puladas: o registro
    de `signal_after_measured_at` é o que torna o disparo idempotente entre
    retentativas do Celery.
    """
    if not event_ids:
        return MeasurementResult()

    drops = list(
        ConnectionDropEvent.objects
        .filter(
            organization=organization,
            pk__in=event_ids,
            restored_at__isnull=False,
            signal_after_measured_at__isnull=True,
        )
        .select_related("connection")
    )
    return _measure_drops(drops, source, now=now)


def measure_now_for_connections(
    organization: Any,
    source: Any,
    *,
    connection_ids: list[int],
    now: datetime | None = None,
) -> MeasurementResult:
    """Ação manual "medir agora" por cliente — o técnico querendo conferir na hora.

    Exposta como função de aplicação porque a tela é de outra tarefa; o que ela
    precisa é disto, não de saber que existe um botão no IXC.

    Diferente do disparo automático, aqui a medição vale mesmo sem queda: mede a
    ONU, atualiza o estado corrente do login e, se houver uma queda recente
    daquele login ainda sem leitura de retorno, aproveita para carimbá-la.
    """
    if not connection_ids:
        return MeasurementResult()

    conexoes = list(
        Connection.objects
        .filter(organization=organization, pk__in=connection_ids)
        .exclude(onu_external_id="")
    )
    ignorados = len(connection_ids) - len(conexoes)
    resultado = _measure_connections(conexoes, source, now=now)
    return MeasurementResult(
        measured=resultado.measured,
        empty=resultado.empty,
        skipped=resultado.skipped + ignorados,
        stopped_early=resultado.stopped_early,
    )


def measure_now_for_outage(
    organization: Any,
    source: Any,
    *,
    outage_id: int,
    now: datetime | None = None,
) -> MeasurementResult:
    """Ação manual "medir agora" para todos os clientes de uma massiva.

    O teto por rodada continua valendo — uma massiva de 44 clientes não vira 44
    consultas simultâneas à OLT porque alguém clicou num botão.
    """
    connection_ids = list(
        OutageAffectedLogin.objects
        .filter(
            organization=organization,
            outage_id=outage_id,
            drop_event__isnull=False,
        )
        .values_list("drop_event__connection_id", flat=True)
    )
    return measure_now_for_connections(
        organization, source, connection_ids=connection_ids, now=now
    )


def _measure_drops(
    drops: list[ConnectionDropEvent], source: Any, *, now: datetime | None
) -> MeasurementResult:
    now = now or timezone.now()
    teto = max_calls_per_round()

    medidos = vazios = ignorados = 0
    vazios_seguidos = 0
    parou_cedo = False

    for drop in drops:
        connection = drop.connection
        if not connection.onu_external_id:
            # Login sem ONU casada: 97,4% dos ativos têm, os outros não têm o que
            # medir. Não é erro, é cobertura.
            ignorados += 1
            continue
        if medidos + vazios >= teto:
            parou_cedo = True
            break

        leitura = source.measure_now(onu_external_id=connection.onu_external_id)
        if leitura is not None:
            _apply_reading(connection, leitura)
        if leitura is None or not leitura.has_signal:
            vazios += 1
            vazios_seguidos += 1
            if vazios_seguidos >= _MAX_CONSECUTIVE_EMPTY:
                # Recuo: leitura vazia em série é sintoma do outro lado.
                parou_cedo = True
                break
            continue

        vazios_seguidos = 0
        drop.signal_rx_after = leitura.signal_rx
        drop.signal_after_measured_at = leitura.measured_at or now
        drop.save(update_fields=[
            "signal_rx_after", "signal_after_measured_at", "updated_at",
        ])
        medidos += 1

    _logger.info(
        "optical_signal_measured",
        measured=medidos,
        empty=vazios,
        skipped=ignorados,
        stopped_early=parou_cedo,
    )
    return MeasurementResult(
        measured=medidos, empty=vazios, skipped=ignorados, stopped_early=parou_cedo
    )


def _measure_connections(
    conexoes: list[Connection], source: Any, *, now: datetime | None
) -> MeasurementResult:
    """Mede por login (ação manual) e carimba a última queda pendente, se houver."""
    now = now or timezone.now()
    teto = max_calls_per_round()

    medidos = vazios = 0
    vazios_seguidos = 0
    parou_cedo = False

    for connection in conexoes:
        if medidos + vazios >= teto:
            parou_cedo = True
            break

        leitura = source.measure_now(onu_external_id=connection.onu_external_id)
        if leitura is None or not leitura.has_signal:
            if leitura is not None:
                # Sem potência, mas pode ter trazido causa/run state: guarda.
                _apply_reading(connection, leitura)
            vazios += 1
            vazios_seguidos += 1
            if vazios_seguidos >= _MAX_CONSECUTIVE_EMPTY:
                parou_cedo = True
                break
            continue

        vazios_seguidos = 0
        _apply_reading(connection, leitura)
        _stamp_recent_drop(connection, leitura, now=now)
        medidos += 1

    return MeasurementResult(
        measured=medidos, empty=vazios, stopped_early=parou_cedo
    )


def _stamp_recent_drop(
    connection: Connection, leitura: Any, *, now: datetime
) -> None:
    """Carimba a queda mais recente do login que ainda não tem leitura de retorno."""
    drop = (
        ConnectionDropEvent.objects
        .filter(connection=connection, signal_after_measured_at__isnull=True)
        .order_by("-dropped_at")
        .first()
    )
    if drop is None:
        return
    drop.signal_rx_after = leitura.signal_rx
    drop.signal_after_measured_at = leitura.measured_at or now
    drop.save(update_fields=[
        "signal_rx_after", "signal_after_measured_at", "updated_at",
    ])


# ---------------------------------------------------------------------------
# Persistência da leitura no login
# ---------------------------------------------------------------------------


def _apply_reading(
    connection: Connection, dto: Any, *, keep_run_state: bool = False
) -> bool:
    """Grava a leitura em `Connection`. Devolve True se algo mudou.

    `keep_run_state=True` na leitura passiva: a listagem não sabe o run state (só
    a medição ativa sabe), e sobrescrevê-lo com vazio apagaria o que a OLT tinha
    dito. Ausência de informação não pode apagar informação.

    O `save` condicional não é micro-otimização: `Connection` tem
    `HistoricalRecords`, e regravar valor igual criaria uma linha de histórico
    por login por rodada para registrar que nada aconteceu.
    """
    novos: dict[str, Any] = {
        "signal_rx": dto.signal_rx,
        "signal_tx": dto.signal_tx,
        "signal_measured_at": dto.measured_at,
        "onu_last_drop_cause": dto.last_drop_cause,
    }
    if dto.onu_external_id:
        novos["onu_external_id"] = dto.onu_external_id
    if not keep_run_state:
        novos["onu_run_state"] = dto.run_state
        novos["onu_last_up_at"] = dto.last_up_at
    elif dto.run_state:
        novos["onu_run_state"] = dto.run_state

    mudou = [f for f, v in novos.items() if getattr(connection, f) != v]
    if not mudou:
        return False
    for field in mudou:
        setattr(connection, field, novos[field])
    connection.save(update_fields=[*mudou, "updated_at"])
    return True
