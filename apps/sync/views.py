"""Views de operação de sync — status + dispatch via Celery."""

from __future__ import annotations

from typing import Any

import structlog
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.integrations.shared.enums import Capability
from apps.integrations.shared.registry import registry
from apps.shared.context import get_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncCheckpoint, SyncJob, SyncMode, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import OrganizationDataSource

_logger = structlog.get_logger(__name__)


def _require_org() -> Any:
    org = get_current_organization()
    if org is None:
        return HttpResponseRedirect("/admin/")
    return org


@allow_cross_tenant(reason="sync status page reads OrganizationDataSource (não-TenantModel)")
def _build_status_rows(organization: Any) -> list[dict[str, Any]]:
    """Para cada (capability, source_type) configurado, agrega status atual."""
    rows: list[dict[str, Any]] = []
    configs = OrganizationDataSource.objects.filter(
        organization=organization, is_active=True
    ).order_by("capability", "-priority")

    for cfg in configs:
        last_job = (
            SyncJob.objects
            .filter(
                organization=organization,
                source_type=cfg.source_type,
                capability=cfg.capability,
            )
            .order_by("-created_at")
            .first()
        )
        checkpoint = (
            SyncCheckpoint.objects
            .filter(
                organization=organization,
                source_type=cfg.source_type,
                capability=cfg.capability,
            )
            .first()
        )
        adapter_registered = (
            registry.get_factory(
                __import__("apps.integrations.shared.enums", fromlist=["SourceType"]).SourceType(cfg.source_type),
                Capability(cfg.capability),
            )
            is not None
        )
        rows.append(
            {
                "datasource_id": cfg.pk,
                "capability": cfg.capability,
                "source_type": cfg.source_type,
                "priority": cfg.priority,
                "adapter_registered": adapter_registered,
                "last_job": last_job,
                "checkpoint": checkpoint,
            }
        )
    return rows


@login_required
@never_cache
def status_page(request: HttpRequest) -> HttpResponse:
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    rows = _build_status_rows(org)
    has_running = any(
        r["last_job"] and r["last_job"].status == SyncStatus.RUNNING for r in rows
    )
    return render(
        request,
        "sync/status.html",
        {"rows": rows, "has_running": has_running, "organization": org},
    )


@login_required
@never_cache
def status_rows_partial(request: HttpRequest) -> HttpResponse:
    """HTMX endpoint — retorna só o <tbody> pra polling refresh."""
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    rows = _build_status_rows(org)
    has_running = any(
        r["last_job"] and r["last_job"].status == SyncStatus.RUNNING for r in rows
    )
    return render(
        request,
        "sync/_status_table_body.html",
        {"rows": rows, "has_running": has_running},
    )


@login_required
@never_cache
@require_POST
def trigger_sync(request: HttpRequest) -> HttpResponse:
    """Dispatcha task Celery async via .delay().

    Worker deve estar rodando: `uv run celery -A config worker --loglevel=info`
    """
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    capability = request.POST.get("capability")
    mode = request.POST.get("mode", SyncMode.INCREMENTAL.value)

    if capability not in Capability.values:
        return HttpResponse(f"Invalid capability: {capability}", status=400)
    if mode not in SyncMode.values:
        return HttpResponse(f"Invalid mode: {mode}", status=400)

    # Dispatcha pra fila do tenant — worker com -Q tenant_<slug> processa.
    # Fallback fila default 'celery' se worker não tiver -Q.
    queue_name = org.celery_queue_name

    task_result = sync_capability.apply_async(
        kwargs={
            "organization_id": org.pk,
            "capability": capability,
            "mode": mode,
        },
        queue=queue_name,
    )

    _logger.info(
        "sync_dispatched_via_ui",
        organization=org.slug,
        capability=capability,
        mode=mode,
        task_id=task_result.id,
        queue=queue_name,
    )

    # HTMX espera 200 com fragment OU redirect via HX-Redirect header
    if request.headers.get("HX-Request"):
        return HttpResponseRedirect(reverse("sync:status_rows"))
    return HttpResponseRedirect(reverse("sync:status"))


@login_required
@never_cache
def trigger_all_sync(request: HttpRequest) -> HttpResponse:  # noqa: ARG001
    """Dispara incremental de TODAS as capabilities ativas pra org atual."""
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    capabilities_to_run = set()
    for cfg in OrganizationDataSource.objects.filter(
        organization=org, is_active=True
    ):
        capabilities_to_run.add(cfg.capability)

    for cap in capabilities_to_run:
        sync_capability.apply_async(
            kwargs={
                "organization_id": org.pk,
                "capability": cap,
                "mode": SyncMode.INCREMENTAL.value,
            },
            queue=org.celery_queue_name,
        )
        _logger.info(
            "sync_all_dispatched",
            organization=org.slug,
            capability=cap,
        )

    return HttpResponseRedirect(reverse("sync:status"))
