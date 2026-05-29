"""Views de operação de sync — status + workers Celery + dispatch."""

from __future__ import annotations

import datetime
from typing import Any

import structlog
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from apps.integrations.shared.enums import Capability, SourceType
from apps.integrations.shared.registry import registry
from apps.shared.context import get_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncCheckpoint, SyncJob, SyncMode, SyncStatus
from apps.sync.tasks import sync_capability
from apps.tenancy.models import OrganizationDataSource

_logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_org() -> Any:
    org = get_current_organization()
    if org is None:
        return HttpResponseRedirect("/admin/")
    return org


def _fmt_duration(started_at: Any, finished_at: Any) -> str | None:
    """Retorna string de duração legível ou None."""
    if not started_at:
        return None
    end = finished_at or timezone.now()
    secs = int((end - started_at).total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h {mins:02d}m"


def _next_beat_run() -> datetime.datetime:
    """Retorna o próximo horário do beat incremental (a cada 3h na hora cheia)."""
    now = timezone.localtime()
    next_hour = (now.hour // 3 + 1) * 3
    if next_hour >= 24:
        next_hour = 0
        next_day = now.date() + datetime.timedelta(days=1)
    else:
        next_day = now.date()
    naive = datetime.datetime.combine(next_day, datetime.time(next_hour, 0))
    return timezone.make_aware(naive)


def _inspect_workers(timeout: float = 1.5) -> dict[str, Any]:
    """
    Consulta workers Celery via inspect(). Retorna dict com worker info.
    Tolerante a falhas — retorna lista vazia se nenhum worker responder.
    """
    try:
        from celery import current_app  # import local evita circular

        inspect = current_app.control.inspect(timeout=timeout)

        # Faz todas as consultas em sequência (cada uma espera `timeout`)
        ping_result = inspect.ping() or {}
        active_result = inspect.active() or {}
        reserved_result = inspect.reserved() or {}
        stats_result = inspect.stats() or {}

        workers: list[dict[str, Any]] = []
        all_hostnames: set[str] = set(ping_result) | set(active_result) | set(stats_result)

        for hostname in sorted(all_hostnames):
            active_tasks = active_result.get(hostname, [])
            reserved_tasks = reserved_result.get(hostname, [])
            worker_stats = stats_result.get(hostname, {})

            # Filas consumidas pelo worker
            queues: list[str] = []
            consumer_queues = (
                worker_stats
                .get("consumer", {})
                .get("queues", [])
            )
            for q in consumer_queues:
                name = q.get("name", "") if isinstance(q, dict) else str(q)
                if name:
                    queues.append(name)

            # Total de tasks processadas
            total = worker_stats.get("total", {})
            total_processed = sum(total.values()) if total else 0

            # Processos no pool
            pool_size = (
                worker_stats.get("pool", {}).get("max-concurrency")
                or worker_stats.get("pool", {}).get("processes", [])
            )
            if isinstance(pool_size, list):
                pool_size = len(pool_size)

            # Tarefa ativa resumida (nome + kwargs relevantes)
            active_summaries = []
            for t in active_tasks:
                kw = t.get("kwargs", {})
                cap = kw.get("capability", "")
                mode = kw.get("mode", "")
                elapsed = None
                if t.get("time_start"):
                    elapsed = _fmt_duration(
                        datetime.datetime.fromtimestamp(t["time_start"], tz=datetime.timezone.utc),
                        None,
                    )
                active_summaries.append({
                    "id": t.get("id", "")[:8],
                    "capability": cap,
                    "mode": mode,
                    "elapsed": elapsed,
                })

            workers.append({
                "hostname": hostname,
                "short_hostname": hostname.split("@")[-1].split(".")[0],
                "online": hostname in ping_result,
                "active_count": len(active_tasks),
                "reserved_count": len(reserved_tasks),
                "active_summaries": active_summaries,
                "total_processed": total_processed,
                "queues": queues,
                "pool_size": pool_size or "?",
            })

        return {
            "workers": workers,
            "worker_count": len(workers),
            "any_online": any(w["online"] for w in workers),
            "error": None,
            "checked_at": timezone.now(),
        }

    except Exception as exc:  # noqa: BLE001
        _logger.warning("celery_inspect_failed", error=str(exc))
        return {
            "workers": [],
            "worker_count": 0,
            "any_online": False,
            "error": str(exc),
            "checked_at": timezone.now(),
        }


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
                SourceType(cfg.source_type),
                Capability(cfg.capability),
            )
            is not None
        )

        # Duração do último job
        duration = None
        if last_job:
            duration = _fmt_duration(last_job.started_at, last_job.finished_at)

        rows.append(
            {
                "datasource_id": cfg.pk,
                "capability": cfg.capability,
                "source_type": cfg.source_type,
                "priority": cfg.priority,
                "adapter_registered": adapter_registered,
                "last_job": last_job,
                "duration": duration,
                "checkpoint": checkpoint,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

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
    next_beat = _next_beat_run()
    now = timezone.now()
    mins_to_beat = int((next_beat - now).total_seconds() // 60)

    return render(
        request,
        "sync/status.html",
        {
            "rows": rows,
            "has_running": has_running,
            "organization": org,
            "next_beat": next_beat,
            "mins_to_beat": mins_to_beat,
        },
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
def worker_status_partial(request: HttpRequest) -> HttpResponse:
    """HTMX endpoint — retorna cards de workers Celery em tempo real (com inspect)."""
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    worker_info = _inspect_workers(timeout=1.5)
    return render(
        request,
        "sync/_workers.html",
        {"worker_info": worker_info, "organization": org},
    )


@login_required
@never_cache
def job_history_partial(request: HttpRequest, capability: str) -> HttpResponse:
    """HTMX endpoint — retorna últimos 10 jobs de uma capability."""
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    if capability not in Capability.values:
        return HttpResponse("Capability inválida", status=400)

    jobs = (
        SyncJob.objects
        .filter(organization=org, capability=capability)
        .order_by("-created_at")[:10]
    )
    jobs_with_duration = [
        {
            "job": j,
            "duration": _fmt_duration(j.started_at, j.finished_at),
        }
        for j in jobs
    ]
    return render(
        request,
        "sync/_job_history.html",
        {"jobs": jobs_with_duration, "capability": capability},
    )


@login_required
@never_cache
@require_POST
def trigger_sync(request: HttpRequest) -> HttpResponse:
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

    if request.headers.get("HX-Request"):
        return HttpResponseRedirect(reverse("sync:status_rows"))
    return HttpResponseRedirect(reverse("sync:status"))


@login_required
@never_cache
def trigger_all_sync(request: HttpRequest) -> HttpResponse:
    """Dispara incremental de TODAS as capabilities ativas pra org atual."""
    org_or_redirect = _require_org()
    if not hasattr(org_or_redirect, "slug"):
        return org_or_redirect
    org = org_or_redirect

    capabilities_to_run: set[str] = set()
    for cfg in OrganizationDataSource.objects.filter(organization=org, is_active=True):
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
        _logger.info("sync_all_dispatched", organization=org.slug, capability=cap)

    return HttpResponseRedirect(reverse("sync:status"))
