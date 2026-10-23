"""Higiene de conexão com o banco fora do ciclo request/response (Celery)."""

from __future__ import annotations

import structlog
from django.db import connections
from psycopg.pq import TransactionStatus

_logger = structlog.get_logger(__name__)


def descarta_conexoes_quebradas() -> int:
    """Fecha toda conexão que não esteja ociosa e utilizável. Devolve quantas.

    O worker roda em prefork e cada processo reaproveita a mesma conexão entre
    tasks. Quando o Celery interrompe uma task no meio de uma query
    (`SoftTimeLimitExceeded`), o psycopg fica com o comando "em andamento" para
    sempre: toda task seguinte naquele processo falha com "another command is
    already in progress" ou "the connection is closed". Foi assim que, em
    21/10/2026, um sync de banda que estourou o prazo derrubou TODAS as tasks do
    worker por 17 horas — o `close_old_connections` do Django não percebe esse
    estado, porque o erro original não foi um `DatabaseError`.

    Fora de um bloco atômico, uma conexão saudável em autocommit está IDLE.
    Qualquer outro estado (ACTIVE, INERROR, UNKNOWN) ou socket fechado = descarta;
    o Django abre outra na próxima query.
    """
    descartadas = 0
    for conn in connections.all(initialized_only=True):
        raw = conn.connection
        if raw is None or conn.in_atomic_block:
            continue
        if not raw.closed and raw.info.transaction_status == TransactionStatus.IDLE:
            continue
        try:
            conn.close()
        except Exception:
            # close() pode reclamar do próprio estado quebrado; o que importa é
            # o Django esquecer o objeto pra abrir um novo na próxima query.
            conn.connection = None
        descartadas += 1
    if descartadas:
        _logger.warning("db_conexao_quebrada_descartada", descartadas=descartadas)
    return descartadas
