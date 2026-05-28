"""Tasks Celery — orquestração de sync.

Princípio (AGENT.md §1.7): sync NÃO conhece IXC nem nenhum sistema externo.
Conversa com adapters via SourceRegistry resolvendo por (org, capability).

Dispatch por capability, não por source — código de sync não muda quando
adapter novo aparece. Adição de nova capability é dispatch case no `_PORT_TO_REPO`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import structlog
from celery import shared_task
from django.utils import timezone

from apps.customers.domain.dto import ContractDTO, CustomerDTO
from apps.customers.infrastructure.repositories import (
    ContractRepository,
    CustomerRepository,
)
from apps.financial.domain.dto import InvoiceDTO, PaymentDTO
from apps.financial.infrastructure.repositories import (
    InvoiceRepository,
    PaymentRepository,
)
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.registry import registry
from apps.shared.context import (
    reset_current_organization,
    set_current_organization,
)
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

from .models import SyncCheckpoint, SyncJob, SyncMode, SyncStatus
from .signals import sync_completed

_logger = structlog.get_logger(__name__)


# =============================================================================
# Beat-scheduled task — itera todas as orgs ativas
# =============================================================================
@shared_task(name="apps.sync.tasks.dispatch_incremental_for_all_orgs")
def dispatch_incremental_for_all_orgs() -> dict[str, int]:
    """Iterates orgs ativas + suas OrganizationDataSources e dispatcha sync
    incremental por (org, capability) na fila tenant específica.

    Agendado via CELERY_BEAT_SCHEDULE a cada 3h. Cada (org, cap) vira uma
    task separada na fila do tenant — paralelismo + isolamento.
    """
    return _dispatch_incremental()


@allow_cross_tenant(reason="beat orchestrator itera Organization (não-TenantModel)")
def _dispatch_incremental() -> dict[str, int]:
    from apps.tenancy.models import OrganizationDataSource

    n_orgs = 0
    n_tasks = 0
    seen_orgs: set[int] = set()
    for cfg in OrganizationDataSource.objects.filter(
        is_active=True, organization__is_active=True
    ).select_related("organization"):
        org = cfg.organization
        if org.pk not in seen_orgs:
            seen_orgs.add(org.pk)
            n_orgs += 1
        sync_capability.apply_async(
            kwargs={
                "organization_id": org.pk,
                "capability": cfg.capability,
                "mode": SyncMode.INCREMENTAL.value,
            },
            queue=org.celery_queue_name,
        )
        n_tasks += 1
        _logger.info(
            "beat_dispatched",
            organization=org.slug,
            capability=cfg.capability,
            source_type=cfg.source_type,
        )
    return {"orgs": n_orgs, "tasks_dispatched": n_tasks}


# =============================================================================
# Mapping capability → (port method, repo factory)
# =============================================================================
# Cada entrada diz: "para sincronizar essa capability, chame esse método no port
# e persista via essa repo". Adicionar capability nova = adicionar entry.

def _customer_port_call(source: Any, since: datetime | None) -> Iterator[CustomerDTO]:
    return source.list_customers(since=since)


def _contract_port_call(source: Any, since: datetime | None) -> Iterator[ContractDTO]:
    return source.list_contracts(since=since)


def _invoice_port_call(source: Any, since: datetime | None) -> Iterator[InvoiceDTO]:
    return source.list_invoices(since=since)


def _payment_port_call(source: Any, since: datetime | None) -> Iterator[PaymentDTO]:
    return source.list_payments(since=since)


def _repo_upsert(repository: Any, dto: Any, source_type: SourceType) -> None:
    """Genérico: todos os repositories expõem o mesmo upsert_from_dto."""
    repository.upsert_from_dto(dto, source_type=source_type)


_DISPATCH: dict[
    Capability,
    tuple[
        Callable[[Any, datetime | None], Iterator[Any]],
        Callable[[Organization], Any],
        Callable[[Any, Any, SourceType], None],
    ],
] = {
    Capability.CUSTOMERS: (_customer_port_call, CustomerRepository, _repo_upsert),
    Capability.CONTRACTS: (_contract_port_call, ContractRepository, _repo_upsert),
    Capability.INVOICES: (_invoice_port_call, InvoiceRepository, _repo_upsert),
    Capability.PAYMENTS: (_payment_port_call, PaymentRepository, _repo_upsert),
}


# =============================================================================
# Task principal
# =============================================================================
@shared_task(
    bind=True,
    name="apps.sync.tasks.sync_capability",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,  # cap 10min entre retries
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
)
def sync_capability(
    self: Any,  # noqa: ARG001
    *,
    organization_id: int,
    capability: str,
    mode: str = SyncMode.INCREMENTAL.value,
) -> dict[str, Any]:
    """Sincroniza uma capability pra uma organização.

    Pra cada adapter ativo (ordenado por priority), itera DTOs do sistema externo
    e persiste via repository do bounded context correspondente.

    Idempotente: bootstrap pode rerodar (compose unique key no DB cuida).
    Incremental usa checkpoint pra reduzir trabalho.

    @allow_cross_tenant: precisa carregar Organization (não-TenantModel) e
    iterar OrganizationDataSource (não-TenantModel). Dentro do escopo do sync,
    seta a org no contextvar pras queries de TenantModel (Customer) operarem
    com escopo correto.
    """
    log = _logger.bind(
        organization_id=organization_id, capability=capability, mode=mode
    )
    log.info("sync_start")

    cap = Capability(capability)
    sync_mode = SyncMode(mode)

    if cap not in _DISPATCH:
        raise ValueError(
            f"Capability {cap.value} não tem dispatch registrado em _DISPATCH"
        )

    return _run_sync(organization_id=organization_id, capability=cap, mode=sync_mode, log=log)


@allow_cross_tenant(reason="sync orchestrator precisa iterar OrganizationDataSource")
def _run_sync(
    *,
    organization_id: int,
    capability: Capability,
    mode: SyncMode,
    log: Any,
) -> dict[str, Any]:
    organization = Organization.objects.get(pk=organization_id)

    # Sources ordenados por priority desc
    sources = registry.get_sources(organization, capability)
    if not sources:
        log.warning("no_sources_configured")
        return {"records_processed": 0, "sources": []}

    port_call, repo_factory, repo_upsert = _DISPATCH[capability]

    total_processed = 0
    per_source_summary: list[dict[str, Any]] = []

    # Seta org no contextvar pra queries de TenantModel (Customer) funcionarem
    token = set_current_organization(organization)
    try:
        for source in sources:
            source_type = source.source_type
            slog = log.bind(source=source_type.value)

            job = SyncJob.objects.create(
                organization=organization,
                source_type=source_type.value,
                capability=capability.value,
                mode=mode.value,
                status=SyncStatus.RUNNING,
                started_at=timezone.now(),
            )

            try:
                checkpoint, _ = SyncCheckpoint.objects.get_or_create(
                    organization=organization,
                    source_type=source_type.value,
                    capability=capability.value,
                )
                since = (
                    checkpoint.last_processed_at
                    if mode == SyncMode.INCREMENTAL
                    else None
                )

                repository = repo_factory(organization)
                count = 0

                for dto in port_call(source, since):
                    repo_upsert(repository, dto, source_type)
                    count += 1

                checkpoint.last_processed_at = timezone.now()
                checkpoint.save(update_fields=["last_processed_at", "updated_at"])

                job.status = SyncStatus.COMPLETED
                job.records_processed = count
                job.finished_at = timezone.now()
                job.save()

                slog.info("source_sync_ok", count=count)
                total_processed += count
                per_source_summary.append(
                    {"source": source_type.value, "count": count, "status": "OK"}
                )

            except Exception as exc:
                job.status = SyncStatus.FAILED
                job.error_message = f"{type(exc).__name__}: {exc}"[:1000]
                job.finished_at = timezone.now()
                job.save()
                slog.error("source_sync_failed", error=str(exc))
                per_source_summary.append(
                    {"source": source_type.value, "status": "FAILED", "error": str(exc)}
                )
                # Não levanta — outros sources podem ainda rodar.

    finally:
        reset_current_organization(token)

    log.info("sync_done", total=total_processed, sources_count=len(sources))
    sync_completed.send(
        sender=None,
        organization=organization,
        capability=capability.value,
        records_processed=total_processed,
    )

    return {
        "records_processed": total_processed,
        "sources": per_source_summary,
    }
