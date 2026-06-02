"""Celery tasks — sync do plano de contas IXC para PlanoContasCache.

Chamadas automáticas via Beat (diário); também invocado pelo management
command `sync_planejamento` quando rodado manualmente.

Padrão idêntico ao apps/sync/tasks.py: dispatcher itera orgs e enfileira
uma sub-task por org na fila do tenant.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task
from django.utils import timezone

from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant

_logger = structlog.get_logger(__name__)


# =============================================================================
# Dispatcher — Beat chama isso; itera todas as orgs IXC ativas
# =============================================================================
@shared_task(name="apps.analytics.tasks.dispatch_plano_contas_for_all_orgs")
def dispatch_plano_contas_for_all_orgs() -> dict[str, int]:
    """Enfileira sync de plano de contas para cada org IXC ativa."""
    return _dispatch_plano()


@allow_cross_tenant(reason="dispatch_plano itera Organization (não-TenantModel)")
def _dispatch_plano() -> dict[str, int]:
    from apps.integrations.shared.enums import Capability, SourceType
    from apps.tenancy.models import OrganizationDataSource

    n = 0
    seen: set[int] = set()
    for ds in (
        OrganizationDataSource.objects
        .filter(
            is_active=True,
            organization__is_active=True,
            source_type=SourceType.IXC.value,
            capability=Capability.CUSTOMERS.value,
        )
        .select_related("organization")
    ):
        org = ds.organization
        if org.pk in seen:
            continue
        seen.add(org.pk)
        sync_plano_contas_for_org.apply_async(
            kwargs={"organization_id": org.pk},
            queue=org.celery_queue_name,
        )
        n += 1
        _logger.info("plano_contas_dispatched", org=org.slug)

    return {"dispatched": n}


# =============================================================================
# Task por org
# =============================================================================
@shared_task(
    name="apps.analytics.tasks.sync_plano_contas_for_org",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def sync_plano_contas_for_org(*, organization_id: int) -> dict[str, Any]:
    """Sincroniza planejamento + planejamento_analitico do IXC para uma org."""
    return _run_plano_sync(organization_id=organization_id)


@allow_cross_tenant(reason="sync_plano_contas opera fora de request HTTP")
def _run_plano_sync(*, organization_id: int) -> dict[str, Any]:
    from apps.analytics.infrastructure.models import PlanoContasCache
    from apps.integrations.ixc.client import IxcHttpClient
    from apps.integrations.shared.enums import Capability, SourceType
    from apps.tenancy.models import Organization, OrganizationDataSource

    org = Organization.objects.get(pk=organization_id)
    set_current_organization(org)
    log = _logger.bind(org=org.slug)

    ds = (
        OrganizationDataSource.objects
        .filter(
            organization=org,
            source_type=SourceType.IXC.value,
            capability=Capability.CUSTOMERS.value,
            is_active=True,
        )
        .first()
    )
    if not ds:
        log.warning("sync_plano_contas_no_credentials")
        return {"skipped": True}

    creds = ds.get_credentials()
    plano_map: dict[str, dict] = {}
    conta_map: dict[str, str] = {}

    with IxcHttpClient(
        base_url=creds["base_url"],
        user_id=creds["user_id"],
        api_token=creds["api_token"],
    ) as client:
        count_plano = 0
        for raw in client.paginate_ixc("planejamento", page_size=200):
            id_plano = str(raw.get("id", "")).strip()
            if not id_plano:
                continue
            plano_map[id_plano] = {
                "cod":  raw.get("cod_planejamento", "").strip(),
                "nome": raw.get("planejamento", "").strip(),
                "tipo": raw.get("tipo", "").strip(),
            }
            count_plano += 1

        count_conta = 0
        for raw in client.paginate_ixc("planejamento_analitico", page_size=500):
            id_conta = str(raw.get("id", "")).strip()
            id_plano = str(raw.get("id_planejamento", "0")).strip()
            if not id_conta:
                continue
            conta_map[id_conta] = id_plano
            count_conta += 1

    plano_map["0"] = {"cod": "", "nome": "(Sem categoria)", "tipo": "?"}
    conta_map["0"] = "0"

    PlanoContasCache.objects.update_or_create(
        organization=org,
        defaults={
            "plano_map": plano_map,
            "conta_map": conta_map,
            "synced_at": timezone.now(),
        },
    )

    log.info(
        "sync_plano_contas_done",
        plano_count=count_plano,
        conta_count=count_conta,
    )
    return {"plano": count_plano, "conta": count_conta}


# =============================================================================
# Churn risk — recomputa scores de risco de cancelamento (Beat diário)
# =============================================================================
@shared_task(name="apps.analytics.tasks.dispatch_churn_risk_for_all_orgs")
def dispatch_churn_risk_for_all_orgs() -> dict[str, int]:
    """Enfileira o recompute de churn risk para cada org ativa."""
    return _dispatch_churn_risk()


@allow_cross_tenant(reason="dispatch_churn_risk itera Organization (não-TenantModel)")
def _dispatch_churn_risk() -> dict[str, int]:
    from apps.tenancy.models import Organization

    n = 0
    for org in Organization.objects.filter(is_active=True):
        compute_churn_risk_for_org.apply_async(
            kwargs={"organization_id": org.pk},
            queue=org.celery_queue_name,
        )
        n += 1
        _logger.info("churn_risk_dispatched", org=org.slug)

    return {"dispatched": n}


@shared_task(
    name="apps.analytics.tasks.compute_churn_risk_for_org",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def compute_churn_risk_for_org(*, organization_id: int) -> dict[str, Any]:
    """Recomputa e persiste os scores de risco de churn de uma org."""
    return _run_churn_risk(organization_id=organization_id)


@allow_cross_tenant(reason="churn risk opera fora de request HTTP")
def _run_churn_risk(*, organization_id: int) -> dict[str, Any]:
    from apps.analytics.application.churn_risk import compute_churn_risk_scores
    from apps.tenancy.models import Organization

    org = Organization.objects.get(pk=organization_id)
    set_current_organization(org)
    return compute_churn_risk_scores(org)
