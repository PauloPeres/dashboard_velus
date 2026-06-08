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

from apps.atendimento.application.sync import run_opa_sync
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import reset_current_organization, set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncCheckpoint
from apps.tenancy.models import OrganizationDataSource

_logger = structlog.get_logger(__name__)

# Janela de carga quando ainda não há checkpoint (1ª execução pós-deploy).
_DEFAULT_WINDOW_DAYS = 90


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

    Mensagens ficam de fora (caras: 1 chamada por atendimento) — o agendado só
    mantém atendimentos/departamentos/vínculos atualizados.
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
            else:
                since = timezone.now() - timedelta(days=_DEFAULT_WINDOW_DAYS)

            started_at = timezone.now()
            source = OpaAtendimentoSource(
                base_url=creds["base_url"], token=creds["token"]
            )
            result = run_opa_sync(org, source, since=since)

            checkpoint.last_processed_at = started_at
            checkpoint.save(update_fields=["last_processed_at", "updated_at"])

            n_atendimentos += result.atendimentos
            log.info(
                "opa_beat_synced",
                atendimentos=result.atendimentos,
                customers_linked=result.customers_linked,
            )
        finally:
            reset_current_organization(token)

    return {"orgs": n_orgs, "atendimentos": n_atendimentos}
