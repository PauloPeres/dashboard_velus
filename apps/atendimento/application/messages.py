"""Busca lazy das mensagens de um atendimento — drill-down do dashboard #49.

A ingestao em massa de mensagens e cara (1 chamada por atendimento na fonte),
entao o sync agendado nao as traz. Quando o gestor abre uma conversa no
dashboard, este modulo busca as mensagens sob demanda na fonte Opa! e as
persiste, pra proxima abertura ser instantanea (cache no proprio banco).
"""

from __future__ import annotations

import structlog

from apps.atendimento.infrastructure.models import Atendimento, Mensagem
from apps.atendimento.infrastructure.repositories import MensagemRepository
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)


def get_or_fetch_messages(
    organization: Organization, atendimento: Atendimento
) -> list[Mensagem]:
    """Retorna as mensagens do atendimento, buscando na fonte se ainda nao houver.

    Idempotente e tolerante a falha: se ja ha mensagens persistidas, nao toca a
    rede; se a fonte falhar (sem datasource, timeout, IPv6, etc.), loga e
    devolve o que existe no banco.
    """
    existing = _stored_messages(organization, atendimento)
    if existing:
        return existing

    ds = OrganizationDataSource.objects.filter(
        organization=organization,
        source_type=SourceType.OPA.value,
        capability=Capability.ATENDIMENTO.value,
        is_active=True,
    ).first()
    if ds is None:
        return existing

    log = _logger.bind(
        organization=organization.slug, atendimento=atendimento.external_id
    )
    try:
        creds = ds.get_credentials()
        source = OpaAtendimentoSource(
            base_url=creds["base_url"], token=creds["token"]
        )
        repo = MensagemRepository(organization)
        for dto in source.list_mensagens(atendimento.external_id):
            repo.upsert_from_dto(dto, source_type=SourceType.OPA)
    except Exception:
        # Drill-down nao pode quebrar por falha de rede (timeout, IPv6, etc.).
        log.warning("opa_lazy_messages_fetch_failed", exc_info=True)
        return _stored_messages(organization, atendimento)

    return _stored_messages(organization, atendimento)


def _stored_messages(
    organization: Organization, atendimento: Atendimento
) -> list[Mensagem]:
    return list(
        Mensagem.objects.filter(
            organization=organization, atendimento=atendimento
        ).order_by("sent_at")
    )
