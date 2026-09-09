"""Tasks Celery do bounded context Atendimento (Opa! Suite).

Opa! NÃO passa pelo SourceRegistry/`_DISPATCH` genérico do `apps.sync`
(decisão de escopo — ver `apps.integrations.opa.apps`): a ingestão tem um
fluxo próprio (`run_opa_sync`) com mapa cliente->documento. Por isso a
recorrência também é dedicada, e não via `dispatch_incremental_for_all_orgs`.

`sync_opa_for_all_orgs` espelha o comando `sync_opasuite`: para cada org com
um OrganizationDataSource OPA/ATENDIMENTO ativo, roda o sync incremental a
partir do checkpoint. Agendada fora do horário comercial (ver
CELERY_BEAT_SCHEDULE) — atendimento não precisa ser realtime.
"""

from __future__ import annotations

from datetime import timedelta

import structlog
from celery import shared_task
from django.utils import timezone

from apps.atendimento.application.mensagens_backfill import run_mensagens_backfill
from apps.atendimento.application.sync import run_opa_sync
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import reset_current_organization, set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncCheckpoint, SyncJob, SyncMode, SyncStatus
from apps.tenancy.models import OrganizationDataSource

_logger = structlog.get_logger(__name__)

# Janela de carga quando ainda não há checkpoint (1ª execução pós-deploy).
_DEFAULT_WINDOW_DAYS = 90
# Mensagens são ~30x mais numerosas que atendimentos: sem checkpoint, uma janela
# de 90 dias no beat seriam ~2.800 chamadas de uma vez. A carga histórica tem
# comando próprio; aqui a janela é curta de propósito.
_DEFAULT_MESSAGE_WINDOW_DAYS = 7


@shared_task(
    name="apps.atendimento.tasks.sync_opa_for_all_orgs",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def sync_opa_for_all_orgs() -> dict[str, int]:
    """Roda o sync Opa! incremental para toda org com datasource OPA ativo.

    Inclui o incremental de mensagens, que roda pela listagem global (~30
    chamadas/dia). O que segue de fora é o `list_mensagens` por atendimento —
    esse sim caro (1 chamada por conversa) e restrito ao drill-down.
    """
    return _sync_opa_for_all_orgs()


@allow_cross_tenant(reason="beat orchestrator itera OrganizationDataSource (não-TenantModel)")
def _sync_opa_for_all_orgs() -> dict[str, int]:
    configs = OrganizationDataSource.objects.filter(
        source_type=SourceType.OPA.value,
        capability=Capability.ATENDIMENTO.value,
        is_active=True,
        organization__is_active=True,
    ).select_related("organization")

    n_orgs = 0
    n_atendimentos = 0
    n_mensagens = 0
    for cfg in configs:
        org = cfg.organization
        n_orgs += 1
        log = _logger.bind(organization=org.slug)
        token = set_current_organization(org)
        try:
            creds = cfg.get_credentials()

            checkpoint, _ = SyncCheckpoint.objects.get_or_create(
                organization=org,
                source_type=SourceType.OPA.value,
                capability=Capability.ATENDIMENTO.value,
            )
            if checkpoint.last_processed_at:
                since = checkpoint.last_processed_at
                mode = SyncMode.INCREMENTAL
            else:
                since = timezone.now() - timedelta(days=_DEFAULT_WINDOW_DAYS)
                mode = SyncMode.BOOTSTRAP

            started_at = timezone.now()
            # SyncJob faz o Opa! aparecer no painel /sync/ como as outras fontes
            # (a recorrência tem fluxo proprio e nao passava pelo sync_capability,
            # entao so mantinha checkpoint — o painel mostrava "nunca rodou").
            job = SyncJob.objects.create(
                organization=org,
                source_type=SourceType.OPA.value,
                capability=Capability.ATENDIMENTO.value,
                mode=mode.value,
                status=SyncStatus.RUNNING,
                started_at=started_at,
            )
            try:
                source = OpaAtendimentoSource(
                    base_url=creds["base_url"], token=creds["token"]
                )
                result = run_opa_sync(org, source, since=since)

                checkpoint.last_processed_at = started_at
                checkpoint.save(update_fields=["last_processed_at", "updated_at"])

                job.status = SyncStatus.COMPLETED
                job.records_processed = result.atendimentos
                job.finished_at = timezone.now()
                job.save()

                n_atendimentos += result.atendimentos
                log.info(
                    "opa_beat_synced",
                    atendimentos=result.atendimentos,
                    customers_linked=result.customers_linked,
                )

                # Mensagens: cursor próprio (capability MENSAGENS) e falha
                # isolada — o volume é um extra, não pode derrubar o sync de
                # atendimentos, que é o que as outras cinco abas leem.
                try:
                    n_mensagens += _sync_mensagens(org, source, log)
                except Exception as exc:
                    log.warning("opa_beat_mensagens_failed", error=str(exc))
            except Exception as exc:
                job.status = SyncStatus.FAILED
                job.error_message = f"{type(exc).__name__}: {exc}"[:1000]
                job.finished_at = timezone.now()
                job.save()
                log.error("opa_beat_failed", error=str(exc))
                raise
        finally:
            reset_current_organization(token)

    return {
        "orgs": n_orgs,
        "atendimentos": n_atendimentos,
        "mensagens": n_mensagens,
    }


def _sync_mensagens(org, source, log) -> int:
    """Incremental do volume de mensagens a partir do checkpoint MENSAGENS.

    Sem checkpoint, pega só a janela padrão em vez de varrer anos de história:
    a carga inicial é trabalho do comando `backfill_opa_mensagens`, que sabe
    fatiar e retomar. Um beat noturno não é lugar de tarefa de horas.
    """
    checkpoint, _ = SyncCheckpoint.objects.get_or_create(
        organization=org,
        source_type=SourceType.OPA.value,
        capability=Capability.MENSAGENS.value,
    )
    since = checkpoint.last_processed_at or (
        timezone.now() - timedelta(days=_DEFAULT_MESSAGE_WINDOW_DAYS)
    )

    result = run_mensagens_backfill(org, source, since=since)

    # Rodada vazia não avança o cursor (#132): sem mensagem nova, `ultima_data`
    # é None e o checkpoint fica onde estava, pra próxima rodada tentar de novo
    # o mesmo trecho em vez de pular por cima de uma fonte quebrada.
    if result.ultima_data is not None:
        checkpoint.last_processed_at = result.ultima_data
        checkpoint.consecutive_empty_runs = 0
        checkpoint.save(
            update_fields=[
                "last_processed_at",
                "consecutive_empty_runs",
                "updated_at",
            ]
        )
    else:
        checkpoint.consecutive_empty_runs += 1
        checkpoint.save(update_fields=["consecutive_empty_runs", "updated_at"])

    log.info("opa_beat_mensagens_synced", mensagens=result.mensagens)
    return result.mensagens
