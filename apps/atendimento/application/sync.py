"""Orquestracao da ingestao Opa! Suite — nucleo testavel, sem I/O de credenciais.

`run_opa_sync` recebe uma `source` ja construida (port `AtendimentoSourcePort`),
o que permite testar com `FakeAtendimentoSource` sem rede. O comando
`sync_opasuite` cuida de credenciais/checkpoint e injeta a source real.

Fluxo:
  1. Sincroniza departamentos (barato).
  2. Monta o mapa `id_cliente_opaco -> documento` a partir da lista de clientes
     (barata) — resolve o vinculo conversa->Customer sem popular atendimento a
     atendimento.
  3. Sincroniza atendimentos, resolvendo o documento via o mapa.
  4. (Opcional) sincroniza mensagens — 1 chamada por atendimento, caro; por isso
     fica atras da flag `with_messages` + `message_limit`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import structlog

from apps.atendimento.domain.ports import AtendimentoSourcePort
from apps.atendimento.infrastructure.repositories import (
    AtendimentoRepository,
    DepartamentoRepository,
    MensagemRepository,
)
from apps.integrations.shared.enums import SourceType
from apps.shared.context import set_current_organization
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class OpaSyncResult:
    departamentos: int = 0
    atendimentos: int = 0
    mensagens: int = 0
    customers_linked: int = 0


def run_opa_sync(
    organization: Organization,
    source: AtendimentoSourcePort,
    *,
    since: datetime | None = None,
    with_messages: bool = False,
    message_limit: int | None = None,
) -> OpaSyncResult:
    """Ingere departamentos, clientes (mapa), atendimentos e (opcional) mensagens.

    `message_limit` limita quantos atendimentos tem as mensagens ingeridas (controle
    de custo) — None = todos quando `with_messages=True`.
    """
    source_type: SourceType = source.source_type

    # Garante o tenant no contextvar — os repositorios (TenantManager) dependem
    # dele. Idempotente com o que o comando ja seta.
    set_current_organization(organization)

    # --- 1. Departamentos ---------------------------------------------------
    dep_repo = DepartamentoRepository(organization)
    dep_count = 0
    for dep in source.list_departamentos():
        dep_repo.upsert_from_dto(dep, source_type=source_type)
        dep_count += 1

    # --- 2. Mapa id_cliente_opaco -> documento ------------------------------
    cliente_map: dict[str, str] = {}
    for ref in source.list_clientes():
        if ref.document:
            cliente_map[ref.external_id] = ref.document

    # --- 2b. Mapa id_atendente_opaco -> nome --------------------------------
    # A listagem de atendimentos so traz o atendente como id opaco; o nome vem
    # da lista de usuarios (barata), igual ao mapa de clientes.
    atendente_map: dict[str, str] = {}
    for ref in source.list_atendentes():
        if ref.nome:
            atendente_map[ref.external_id] = ref.nome

    # --- 3. Atendimentos ----------------------------------------------------
    at_repo = AtendimentoRepository(organization)
    msg_repo = MensagemRepository(organization)
    at_count = 0
    linked = 0
    msg_count = 0
    populated = 0

    for dto in source.list_atendimentos(since=since):
        document = dto.customer_document or cliente_map.get(dto.customer_external_id, "")
        if document and document != dto.customer_document:
            dto = replace(dto, customer_document=document)

        nome = dto.atendente_nome or atendente_map.get(dto.atendente_external_id, "")
        if nome and nome != dto.atendente_nome:
            dto = replace(dto, atendente_nome=nome)

        atendimento, _created = at_repo.upsert_from_dto(dto, source_type=source_type)
        at_count += 1
        if atendimento.customer_id is not None:
            linked += 1

        # --- 4. Mensagens (opcional, caro) ---
        if with_messages and (message_limit is None or populated < message_limit):
            populated += 1
            for msg in source.list_mensagens(dto.external_id):
                msg_repo.upsert_from_dto(msg, source_type=source_type)
                msg_count += 1

    result = OpaSyncResult(
        departamentos=dep_count,
        atendimentos=at_count,
        mensagens=msg_count,
        customers_linked=linked,
    )
    _logger.info(
        "opa_sync_done",
        org=organization.slug,
        departamentos=result.departamentos,
        atendimentos=result.atendimentos,
        mensagens=result.mensagens,
        customers_linked=result.customers_linked,
        with_messages=with_messages,
    )
    return result
