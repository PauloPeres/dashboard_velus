"""Ingestao em massa das mensagens — o volume que a pagina de Mensagens le.

Por que existe, se `messages.get_or_fetch_messages` ja busca mensagens: aquilo e
drill-down (1 chamada por conversa, sob demanda). Medir volume por esse caminho
custaria uma chamada por atendimento e, pior, o que ja estava no banco era so o
que alguem tinha clicado — 3,5% da base, enviesado pras conversas ruins. Grafico
de volume feito em cima daquilo mede o habito de clique do gestor, nao a
operacao.

A saida e a listagem global de `atendimento/mensagem`: a Opa aceita listar sem
`id_rota` e a colecao e append-only em ordem de insercao, entao paginar por
`skip` cobre a conta inteira em ~6,7 mil chamadas (contra ~34 mil) e o
incremental diario fica em algumas dezenas.

Duas decisoes que valem registro:

- **So metadado, sem texto.** Direcao, tipo, canal, data e janela de 24h
  respondem tudo que a pagina pergunta; o texto seriam centenas de milhares de
  mensagens de cliente (PII) no dashboard sem nenhuma pergunta que dependesse
  delas. O drill-down continua buscando o texto sob demanda.
- **O cursor e a data, nao o offset.** O offset e valido so dentro de uma
  rodada; entre rodadas ele envelhece (a colecao cresce). Guardando a data da
  ultima mensagem processada, a rodada seguinte reencontra a posicao por
  bissecao (~22 chamadas) e se autocorrige mesmo depois de uma interrupcao no
  meio do backfill.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import structlog

from apps.atendimento.domain.dto import MensagemDTO
from apps.atendimento.domain.ports import AtendimentoSourcePort
from apps.atendimento.infrastructure.repositories import MensagemRepository
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

# Mensagens por INSERT. 100 = uma pagina da API; subir daqui nao acelera (o
# gargalo e a rede), so aumenta o que se perde numa interrupcao.
_BATCH_SIZE = 500


@dataclass(frozen=True)
class MensagensBackfillResult:
    mensagens: int = 0
    paginas: int = 0
    ultimo_offset: int = 0
    ultima_data: datetime | None = None
    # False quando parou por `max_pages` — ha mais historia pra frente.
    concluido: bool = True


def run_mensagens_backfill(
    organization: Organization,
    source: AtendimentoSourcePort,
    *,
    since: datetime | None = None,
    start_skip: int | None = None,
    max_pages: int | None = None,
    page_size: int = 100,
    on_progress: Callable[[MensagensBackfillResult], None] | None = None,
    progress_every: int = 50,
) -> MensagensBackfillResult:
    """Ingere mensagens (so metadado) da listagem global, a partir de `since`.

    `start_skip` pula a bissecao quando o chamador ja sabe o offset (retomada
    manual); sem ele, `since` e traduzido em offset pela fonte. Sem nenhum dos
    dois, comeca do inicio da colecao.

    `on_progress` e chamado a cada `progress_every` paginas com o parcial — e
    por onde o comando persiste o checkpoint e imprime andamento, sem que este
    modulo precise conhecer SyncCheckpoint.
    """
    set_current_organization(organization)
    source_type = source.source_type

    if start_skip is None:
        start_skip = source.find_skip_for_date(since) if since is not None else 0

    repo = MensagemRepository(organization)
    log = _logger.bind(org=organization.slug, start_skip=start_skip)
    log.info("opa_mensagens_backfill_start", since=since)

    lote: list[MensagemDTO] = []
    total = 0
    paginas = 0
    ultimo_offset = start_skip
    ultima_data: datetime | None = None
    paginas_no_progresso = 0

    def _flush() -> None:
        nonlocal lote, total
        if lote:
            total += repo.bulk_upsert_metadata(lote, source_type=source_type)
            lote = []

    for offset, dto in source.list_mensagens_global(
        start_skip=start_skip, page_size=page_size, max_pages=max_pages
    ):
        lote.append(dto)
        ultimo_offset = offset
        if dto.sent_at is not None:
            ultima_data = dto.sent_at

        if len(lote) >= _BATCH_SIZE:
            _flush()

        # `offset` e absoluto: a virada de pagina e quando ele cruza o tamanho
        # de pagina a partir do inicio.
        if (offset - start_skip + 1) % page_size == 0:
            paginas += 1
            paginas_no_progresso += 1
            if on_progress is not None and paginas_no_progresso >= progress_every:
                paginas_no_progresso = 0
                _flush()
                on_progress(
                    MensagensBackfillResult(
                        mensagens=total,
                        paginas=paginas,
                        ultimo_offset=ultimo_offset,
                        ultima_data=ultima_data,
                        concluido=False,
                    )
                )

    _flush()
    result = MensagensBackfillResult(
        mensagens=total,
        paginas=paginas,
        ultimo_offset=ultimo_offset,
        ultima_data=ultima_data,
        concluido=max_pages is None or paginas < max_pages,
    )
    log.info(
        "opa_mensagens_backfill_done",
        mensagens=result.mensagens,
        paginas=result.paginas,
        ultima_data=result.ultima_data,
        concluido=result.concluido,
    )
    return result
