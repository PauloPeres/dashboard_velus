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
from apps.financial.domain.dto import ExpenseDTO, InvoiceDTO, PaymentDTO
from apps.financial.infrastructure.repositories import (
    ExpenseRepository,
    InvoiceRepository,
    PaymentRepository,
)
from apps.helpdesk.domain.dto import TicketDTO
from apps.helpdesk.infrastructure.repositories import TicketRepository
from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.registry import registry
from apps.inventory.domain.dto import EquipmentDTO
from apps.inventory.infrastructure.repositories import EquipmentRepository
from apps.network.domain.dto import BandwidthUsageDTO, ConnectionDTO
from apps.network.infrastructure.repositories import (
    BandwidthUsageRepository,
    ConnectionRepository,
)
from apps.sales.domain.dto import LeadDTO, OpportunityDTO
from apps.sales.infrastructure.repositories import (
    LeadRepository,
    OpportunityRepository,
)
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
def dispatch_incremental_for_all_orgs(
    capabilities: list[str] | None = None,
) -> dict[str, int]:
    """Iterates orgs ativas + suas OrganizationDataSources e dispatcha sync
    incremental por (org, capability) na fila tenant específica.

    Agendado via CELERY_BEAT_SCHEDULE. Cada (org, cap) vira uma task separada
    na fila do tenant — paralelismo + isolamento.

    `capabilities` (opcional) restringe o dispatch a um subconjunto de
    capabilities. O Beat escalona grupos de capabilities em janelas distintas
    pra não disparar as 11 sincronizações ao mesmo tempo e sobrecarregar a API
    do IXC. Sem o filtro (None), dispatcha todas — útil pra disparo manual.
    """
    return _dispatch_incremental(capabilities)


@allow_cross_tenant(reason="beat orchestrator itera Organization (não-TenantModel)")
def _dispatch_incremental(capabilities: list[str] | None = None) -> dict[str, int]:
    from apps.tenancy.models import OrganizationDataSource

    configs = OrganizationDataSource.objects.filter(
        is_active=True, organization__is_active=True
    )
    if capabilities:
        configs = configs.filter(capability__in=capabilities)

    n_orgs = 0
    n_tasks = 0
    seen_orgs: set[int] = set()
    for cfg in configs.select_related("organization"):
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


def _expense_port_call(source: Any, since: datetime | None) -> Iterator[ExpenseDTO]:
    return source.list_expenses(since=since)


def _ticket_port_call(source: Any, since: datetime | None) -> Iterator[TicketDTO]:
    return source.list_tickets(since=since)


def _connection_port_call(source: Any, since: datetime | None) -> Iterator[ConnectionDTO]:
    return source.list_connections(since=since)


def _bandwidth_port_call(
    source: Any, since: datetime | None
) -> Iterator[BandwidthUsageDTO]:
    return source.list_bandwidth_usage(since=since)


def _equipment_port_call(source: Any, since: datetime | None) -> Iterator[EquipmentDTO]:
    return source.list_equipment(since=since)


def _lead_port_call(source: Any, since: datetime | None) -> Iterator[LeadDTO]:
    return source.list_leads(since=since)


def _opportunity_port_call(source: Any, since: datetime | None) -> Iterator[OpportunityDTO]:
    return source.list_opportunities(since=since)


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
    Capability.EXPENSES: (_expense_port_call, ExpenseRepository, _repo_upsert),
    Capability.TICKETS: (_ticket_port_call, TicketRepository, _repo_upsert),
    Capability.CONNECTIONS: (_connection_port_call, ConnectionRepository, _repo_upsert),
    Capability.BANDWIDTH: (_bandwidth_port_call, BandwidthUsageRepository, _repo_upsert),
    Capability.EQUIPMENT: (_equipment_port_call, EquipmentRepository, _repo_upsert),
    Capability.LEADS: (_lead_port_call, LeadRepository, _repo_upsert),
    Capability.OPPORTUNITIES: (_opportunity_port_call, OpportunityRepository, _repo_upsert),
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
                skipped = 0

                for dto in port_call(source, since):
                    try:
                        repo_upsert(repository, dto, source_type)
                        count += 1
                    except Exception as record_exc:
                        skipped += 1
                        slog.warning(
                            "sync_record_skipped",
                            error=f"{type(record_exc).__name__}: {record_exc}"[:200],
                            skipped_total=skipped,
                        )
                        if skipped > 100:
                            slog.error("sync_too_many_skips", skipped=skipped)
                            raise

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


# =============================================================================
# Reconciliação — detecta REMOÇÕES no IXC (fonte da verdade)
# =============================================================================
# O sync incremental é upsert-only e filtra por data de emissão/baixa, então
# nunca percebe um registro DELETADO no IXC (ex.: carnê de pagamento removido,
# despesa apagada). A reconciliação faz um pull COMPLETO por capability,
# coleta os external_ids vistos e faz soft-delete dos ativos locais ausentes
# (nosso sistema nunca remove fisicamente — só marca `deleted_at`).
#
# Só PAYMENTS e EXPENSES têm soft-delete (repos com `soft_delete_missing`).
_RECONCILABLE = frozenset({Capability.PAYMENTS, Capability.EXPENSES})

# Capabilities cuja reconciliação RE-UPSERTA cada registro do pull completo —
# necessário pra capturar mudanças in-place (ex.: Expense cancelada, status C)
# que o incremental, filtrado por data de emissão, não reprocessa. PAYMENTS
# fica de fora: volume alto e sem campo de status; re-salvar tudo toda noite
# explodiria as tabelas de history. Pra Payment só a detecção por ausência importa.
_RECONCILE_REUPSERT = frozenset({Capability.EXPENSES})


@shared_task(name="apps.sync.tasks.dispatch_reconciliation_for_all_orgs")
def dispatch_reconciliation_for_all_orgs(
    capabilities: list[str] | None = None,
) -> dict[str, int]:
    """Beat chama isso (madrugada): enfileira reconciliação por (org, capability)."""
    caps = capabilities or [c.value for c in _RECONCILABLE]
    return _dispatch_reconciliation(caps)


@allow_cross_tenant(reason="reconciliation orchestrator itera Organization (não-TenantModel)")
def _dispatch_reconciliation(capabilities: list[str]) -> dict[str, int]:
    from apps.tenancy.models import OrganizationDataSource

    configs = OrganizationDataSource.objects.filter(
        is_active=True,
        organization__is_active=True,
        capability__in=capabilities,
    ).select_related("organization")

    n_tasks = 0
    seen: set[tuple[int, str]] = set()
    for cfg in configs:
        org = cfg.organization
        key = (org.pk, cfg.capability)
        if key in seen:  # reconcile_capability já itera todos os sources da cap
            continue
        seen.add(key)
        reconcile_capability.apply_async(
            kwargs={"organization_id": org.pk, "capability": cfg.capability},
            queue=org.celery_queue_name,
        )
        n_tasks += 1
        _logger.info(
            "reconciliation_dispatched",
            organization=org.slug,
            capability=cfg.capability,
        )
    return {"tasks_dispatched": n_tasks}


@shared_task(
    bind=True,
    name="apps.sync.tasks.reconcile_capability",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=2,
    acks_late=True,
)
def reconcile_capability(
    self: Any,  # noqa: ARG001
    *,
    organization_id: int,
    capability: str,
) -> dict[str, Any]:
    """Reconcilia uma capability: pull completo + soft-delete por ausência."""
    log = _logger.bind(organization_id=organization_id, capability=capability)
    log.info("reconcile_start")

    cap = Capability(capability)
    if cap not in _RECONCILABLE:
        raise ValueError(f"Capability {cap.value} não é reconciliável")

    return _run_reconciliation(organization_id=organization_id, capability=cap, log=log)


@allow_cross_tenant(reason="reconciliation precisa iterar OrganizationDataSource")
def _run_reconciliation(
    *,
    organization_id: int,
    capability: Capability,
    log: Any,
) -> dict[str, Any]:
    organization = Organization.objects.get(pk=organization_id)

    sources = registry.get_sources(organization, capability)
    if not sources:
        log.warning("no_sources_configured")
        return {"records_processed": 0, "sources": []}

    port_call, repo_factory, _ = _DISPATCH[capability]
    reupsert = capability in _RECONCILE_REUPSERT

    total_seen = 0
    per_source_summary: list[dict[str, Any]] = []

    token = set_current_organization(organization)
    try:
        for source in sources:
            source_type = source.source_type
            slog = log.bind(source=source_type.value)

            job = SyncJob.objects.create(
                organization=organization,
                source_type=source_type.value,
                capability=capability.value,
                mode=SyncMode.BOOTSTRAP.value,  # pull completo; NÃO mexe no checkpoint
                status=SyncStatus.RUNNING,
                started_at=timezone.now(),
            )

            try:
                repository = repo_factory(organization)
                seen_external_ids: set[str] = set()
                skipped = 0

                for dto in port_call(source, None):  # since=None → pull completo
                    seen_external_ids.add(dto.external_id)
                    if reupsert:
                        try:
                            repository.upsert_from_dto(dto, source_type=source_type)
                        except Exception as record_exc:
                            skipped += 1
                            slog.warning(
                                "reconcile_record_skipped",
                                error=f"{type(record_exc).__name__}: {record_exc}"[:200],
                                skipped_total=skipped,
                            )
                            if skipped > 100:
                                slog.error("reconcile_too_many_skips", skipped=skipped)
                                raise

                result = repository.soft_delete_missing(
                    seen_external_ids=seen_external_ids,
                    source_type=source_type,
                )

                job.status = SyncStatus.COMPLETED
                job.records_processed = len(seen_external_ids)
                job.finished_at = timezone.now()
                if result.get("aborted"):
                    job.error_message = (
                        f"guard-rail: pull trouxe {result['seen']} < "
                        f"{result['active']} ativos — soft-delete abortado"
                    )
                job.save()

                slog.info("source_reconcile_ok", **result)
                total_seen += len(seen_external_ids)
                per_source_summary.append(
                    {"source": source_type.value, "status": "OK", **result}
                )

            except Exception as exc:
                job.status = SyncStatus.FAILED
                job.error_message = f"{type(exc).__name__}: {exc}"[:1000]
                job.finished_at = timezone.now()
                job.save()
                slog.error("source_reconcile_failed", error=str(exc))
                per_source_summary.append(
                    {"source": source_type.value, "status": "FAILED", "error": str(exc)}
                )

    finally:
        reset_current_organization(token)

    log.info("reconcile_done", total_seen=total_seen, sources_count=len(sources))
    # Dispara rebuild das fact (que dropa as linhas dos soft-deleted).
    sync_completed.send(
        sender=None,
        organization=organization,
        capability=capability.value,
        records_processed=total_seen,
    )

    return {"records_processed": total_seen, "sources": per_source_summary}
