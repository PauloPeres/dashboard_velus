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
    from apps.analytics.infrastructure.models import FornecedorCache, PlanoContasCache
    from apps.integrations.ixc.client import IxcHttpClient
    from apps.integrations.ixc.expenses import IxcSupplierCache
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

        # Fornecedores — mesma cadência/credenciais; resolve nomes no DRE-Contas.
        supplier_map = IxcSupplierCache(client).load_all()

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
    FornecedorCache.objects.update_or_create(
        organization=org,
        defaults={
            "supplier_map": supplier_map,
            "synced_at": timezone.now(),
        },
    )

    log.info(
        "sync_plano_contas_done",
        plano_count=count_plano,
        conta_count=count_conta,
        supplier_count=len(supplier_map),
    )
    return {"plano": count_plano, "conta": count_conta, "fornecedor": len(supplier_map)}


# =============================================================================
# Rebuild de fact financeiras — rede de segurança agendada (Beat diário)
# =============================================================================
@shared_task(name="apps.analytics.tasks.dispatch_fact_rebuild_for_all_orgs")
def dispatch_fact_rebuild_for_all_orgs() -> dict[str, int]:
    """Enfileira o rebuild das fact financeiras para cada org ativa."""
    return _dispatch_fact_rebuild()


@allow_cross_tenant(reason="dispatch_fact_rebuild itera Organization (não-TenantModel)")
def _dispatch_fact_rebuild() -> dict[str, int]:
    from apps.tenancy.models import Organization

    n = 0
    for org in Organization.objects.filter(is_active=True):
        rebuild_financial_facts_for_org.apply_async(
            kwargs={"organization_id": org.pk},
            queue=org.celery_queue_name,
        )
        n += 1
        _logger.info("fact_rebuild_dispatched", org=org.slug)

    return {"dispatched": n}


@shared_task(
    name="apps.analytics.tasks.rebuild_financial_facts_for_org",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def rebuild_financial_facts_for_org(*, organization_id: int) -> dict[str, Any]:
    """Rematerializa fatura/pagamento/despesa fact de uma org."""
    return _run_fact_rebuild(organization_id=organization_id)


@allow_cross_tenant(reason="rebuild de fact opera fora de request HTTP")
def _run_fact_rebuild(*, organization_id: int) -> dict[str, Any]:
    from apps.analytics.application.rebuild import rebuild_financial_facts
    from apps.tenancy.models import Organization

    org = Organization.objects.get(pk=organization_id)
    set_current_organization(org)
    summary = rebuild_financial_facts(org)
    _logger.info("fact_rebuild_done", org=org.slug, **summary)
    return summary


# =============================================================================
# Snapshot de rede — captura métricas de conexão/banda pra série temporal (Beat)
# =============================================================================
@shared_task(name="apps.analytics.tasks.dispatch_network_snapshot_for_all_orgs")
def dispatch_network_snapshot_for_all_orgs() -> dict[str, int]:
    """Enfileira a captura do snapshot de rede para cada org ativa."""
    return _dispatch_network_snapshot()


@allow_cross_tenant(reason="dispatch_network_snapshot itera Organization (não-TenantModel)")
def _dispatch_network_snapshot() -> dict[str, int]:
    from apps.tenancy.models import Organization

    n = 0
    for org in Organization.objects.filter(is_active=True):
        capture_network_snapshot_for_org.apply_async(
            kwargs={"organization_id": org.pk},
            queue=org.celery_queue_name,
        )
        n += 1
        _logger.info("network_snapshot_dispatched", org=org.slug)

    return {"dispatched": n}


@shared_task(
    name="apps.analytics.tasks.capture_network_snapshot_for_org",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def capture_network_snapshot_for_org(*, organization_id: int) -> dict[str, Any]:
    """Captura e persiste 1 snapshot de métricas de rede de uma org."""
    return _run_network_snapshot(organization_id=organization_id)


@allow_cross_tenant(reason="snapshot de rede opera fora de request HTTP")
def _run_network_snapshot(*, organization_id: int) -> dict[str, Any]:
    from apps.analytics.application.network_snapshots import capture_network_snapshot
    from apps.tenancy.models import Organization

    org = Organization.objects.get(pk=organization_id)
    set_current_organization(org)
    snap = capture_network_snapshot(org)
    summary = {
        "total": snap.total_count,
        "online": snap.online_count,
        "offline": snap.offline_count,
    }
    _logger.info("network_snapshot_done", org=org.slug, **summary)
    return summary


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


# =============================================================================
# Churn ML — treino do modelo de churn por org (Beat semanal)
# =============================================================================
@shared_task(name="apps.analytics.tasks.dispatch_churn_ml_train_for_all_orgs")
def dispatch_churn_ml_train_for_all_orgs() -> dict[str, int]:
    """Enfileira o (re)treino do modelo de churn ML para cada org ativa."""
    return _dispatch_churn_ml_train()


@allow_cross_tenant(reason="dispatch_churn_ml itera Organization (não-TenantModel)")
def _dispatch_churn_ml_train() -> dict[str, int]:
    from apps.tenancy.models import Organization

    n = 0
    for org in Organization.objects.filter(is_active=True):
        train_churn_model_for_org.apply_async(
            kwargs={"organization_id": org.pk},
            queue=org.celery_queue_name,
        )
        n += 1
        _logger.info("churn_ml_train_dispatched", org=org.slug)

    return {"dispatched": n}


@shared_task(
    name="apps.analytics.tasks.train_churn_model_for_org",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def train_churn_model_for_org(*, organization_id: int) -> dict[str, Any]:
    """Treina e persiste o modelo de churn ML de uma org."""
    return _run_churn_ml_train(organization_id=organization_id)


@allow_cross_tenant(reason="treino churn ML opera fora de request HTTP")
def _run_churn_ml_train(*, organization_id: int) -> dict[str, Any]:
    from apps.analytics.application.churn_ml import train_churn_model
    from apps.tenancy.models import Organization

    org = Organization.objects.get(pk=organization_id)
    set_current_organization(org)
    summary = train_churn_model(org)
    return summary or {"skipped": True}


# =============================================================================
# Churn digest — envia digest de risco por email aos usuários opt-in
# =============================================================================
@shared_task(name="apps.analytics.tasks.dispatch_churn_digest_weekly")
def dispatch_churn_digest_weekly() -> dict[str, int]:
    """Enfileira o envio do digest semanal de churn por org ativa."""
    return _dispatch_churn_digest("weekly")


@shared_task(name="apps.analytics.tasks.dispatch_churn_digest_monthly")
def dispatch_churn_digest_monthly() -> dict[str, int]:
    """Enfileira o envio do digest mensal de churn por org ativa."""
    return _dispatch_churn_digest("monthly")


@allow_cross_tenant(reason="dispatch_churn_digest itera Organization (não-TenantModel)")
def _dispatch_churn_digest(period: str) -> dict[str, int]:
    from apps.tenancy.models import Organization

    n = 0
    for org in Organization.objects.filter(is_active=True):
        send_churn_digest_for_org.apply_async(
            kwargs={"organization_id": org.pk, "period": period},
            queue=org.celery_queue_name,
        )
        n += 1
        _logger.info("churn_digest_dispatched", org=org.slug, period=period)

    return {"dispatched": n}


@shared_task(
    name="apps.analytics.tasks.send_churn_digest_for_org",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    max_retries=2,
    acks_late=True,
)
def send_churn_digest_for_org(*, organization_id: int, period: str) -> dict[str, Any]:
    """Envia o digest de churn de uma org aos usuários opt-in."""
    return _run_churn_digest(organization_id=organization_id, period=period)


@allow_cross_tenant(reason="envio de digest opera fora de request HTTP")
def _run_churn_digest(*, organization_id: int, period: str) -> dict[str, Any]:
    from apps.analytics.application.churn_digest import send_churn_digest
    from apps.tenancy.models import Organization

    org = Organization.objects.get(pk=organization_id)
    set_current_organization(org)
    return send_churn_digest(org, period)
