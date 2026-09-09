"""Tasks Celery do bounded context Network — poll de status de conexão (#144).

Cadência de minutos, não de horas: o sync completo de rede (6h, via
`apps.sync.tasks`) continua como está e responde "qual é a planta"; este poll
responde "quem está fora AGORA", que é a única pergunta que a aba de massivas
faz. A leitura é uma chamada de listagem (~240 linhas) — barata de propósito.

Este módulo é o composition root do poll: é aqui, e só aqui, que `apps.network`
encosta em `apps.integrations` (pelo `SourceRegistry`, como o resto do projeto).
A camada de aplicação recebe o adapter já resolvido e não sabe quem o
implementa.
"""

from __future__ import annotations

from typing import Any

import structlog
from celery import shared_task

from apps.integrations.shared.enums import Capability
from apps.integrations.shared.registry import registry
from apps.network.application.connection_poll import PollResult, run_connection_poll
from apps.network.application.optical_signal import (
    capture_drop_baselines,
    measure_after_restore,
    measure_delay_seconds,
    measure_now_for_connections,
    measure_now_for_outage,
    refresh_optical_signals,
)
from apps.shared.context import (
    reset_current_organization,
    set_current_organization,
)
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)


@shared_task(name="apps.network.tasks.dispatch_connection_poll_for_all_orgs")
def dispatch_connection_poll_for_all_orgs() -> dict[str, int]:
    """Beat a cada 3 min: dispara o poll na fila de cada org com CONNECTIONS ativo."""
    return _dispatch_connection_poll()


@allow_cross_tenant(reason="beat orchestrator itera OrganizationDataSource (não-TenantModel)")
def _dispatch_connection_poll() -> dict[str, int]:
    from apps.tenancy.models import OrganizationDataSource

    configs = OrganizationDataSource.objects.filter(
        is_active=True,
        organization__is_active=True,
        capability=Capability.CONNECTIONS.value,
    ).select_related("organization")

    orgs = {cfg.organization.pk: cfg.organization for cfg in configs}
    for org in orgs.values():
        poll_connection_status.apply_async(
            kwargs={"organization_id": org.pk}, queue=org.celery_queue_name
        )
    return {"orgs": len(orgs)}


@shared_task(name="apps.network.tasks.poll_connection_status")
def poll_connection_status(organization_id: int) -> dict[str, Any]:
    """Uma rodada do poll para uma organização.

    **Nunca levanta.** Sem retry e sem propagar exceção de propósito: o beat
    volta em 3 minutos, e uma leitura ruim do IXC (host dual-stack sem rota IPv6
    no cluster, ou a página HTML de erro que a API às vezes devolve com HTTP
    200) não pode derrubar a recorrência nem virar retry acumulado. A rodada que
    falhou é descartada inteira pela camada de aplicação — não se conclui de uma
    leitura ausente que ninguém caiu.
    """
    organization = Organization.objects.get(pk=organization_id)
    log = _logger.bind(organization=organization.slug)

    sources = registry.get_sources(organization, Capability.CONNECTIONS)
    if not sources:
        log.warning("connection_poll_no_sources")
        return {"failed": True, "reason": "no_sources"}

    # Setar a org no contextvar é obrigatório: todo TenantModel tocado aqui
    # (Connection, ConnectionDropEvent, OutageEvent) passa pelo TenantManager,
    # que levanta sem organização no contexto.
    token = set_current_organization(organization)
    try:
        # Um source só — o poll é status corrente, e duas fontes disputando o
        # mesmo login abririam e fechariam a mesma queda em alternância.
        result = run_connection_poll(organization, sources[0])
    except Exception as exc:
        log.error("connection_poll_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return {"failed": True, "reason": type(exc).__name__}
    else:
        # Só depois de uma rodada boa: o enriquecimento de sinal se apoia em
        # "quem voltou", e uma leitura que falhou não diz que alguém voltou.
        _optical_signal_follow_up(organization, result)
    finally:
        reset_current_organization(token)

    return {
        "failed": result.failed,
        "read": result.read,
        "opened": result.opened,
        "closed": result.closed,
        "outages_detected": result.outages_detected,
        "outages_opened": result.outages_opened,
        "outages_closed": result.outages_closed,
    }


# ---------------------------------------------------------------------------
# Sinal óptico da ONU (#148)
# ---------------------------------------------------------------------------


def _optical_signal_follow_up(organization: Organization, result: PollResult) -> None:
    """Enriquecimento de sinal depois de uma rodada de poll bem-sucedida.

    Duas coisas, e as duas custam o que devem custar:

    1. **linha de base** das quedas recém-abertas — leitura de listagem do ERP,
       não toca na OLT;
    2. **agendamento** da medição ativa para quem voltou ao ar. Note o
       `countdown`: a ONU acabou de subir e precisa estabilizar antes de ser
       medida, senão o valor registrado é o do transitório e não o do enlace
       reparado.

    Nada acontece se a organização não habilitou a capability OPTICAL_SIGNAL —
    ela autoriza consultar equipamento de produção e por isso é opt-in explícito.
    Nada acontece, tampouco, se ninguém voltou: **sem retorno, zero chamadas**.

    Nunca levanta: sinal é enriquecimento, e o poll de status é o dado principal.
    """
    if result.failed:
        return

    sources = registry.get_sources(organization, Capability.OPTICAL_SIGNAL)
    if not sources:
        return

    log = _logger.bind(organization=organization.slug)
    try:
        capture_drop_baselines(organization, sources[0])
    except Exception as exc:
        log.warning(
            "optical_signal_baseline_failed",
            error=f"{type(exc).__name__}: {exc}"[:200],
        )

    if not result.restored_event_ids:
        return

    measure_optical_signal_after_restore.apply_async(
        kwargs={
            "organization_id": organization.pk,
            "event_ids": list(result.restored_event_ids),
        },
        queue=organization.celery_queue_name,
        countdown=measure_delay_seconds(),
    )


@shared_task(name="apps.network.tasks.measure_optical_signal_after_restore")
def measure_optical_signal_after_restore(
    organization_id: int, event_ids: list[int]
) -> dict[str, Any]:
    """Mede o sinal das ONUs que voltaram ao ar — uma vez por queda.

    **Sem retry.** Do outro lado tem a OLT: uma medição perdida é aceitável,
    martelar equipamento de produção não é. E o `signal_after_measured_at` já
    gravado torna a tarefa idempotente se o Celery reentregar a mensagem.
    """
    organization = Organization.objects.get(pk=organization_id)
    log = _logger.bind(organization=organization.slug)

    sources = registry.get_sources(organization, Capability.OPTICAL_SIGNAL)
    if not sources:
        return {"measured": 0, "reason": "no_sources"}

    token = set_current_organization(organization)
    try:
        result = measure_after_restore(
            organization, sources[0], event_ids=list(event_ids or [])
        )
    except Exception as exc:
        log.error(
            "optical_signal_measure_failed",
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return {"measured": 0, "reason": type(exc).__name__}
    finally:
        reset_current_organization(token)

    return {
        "measured": result.measured,
        "empty": result.empty,
        "skipped": result.skipped,
        "stopped_early": result.stopped_early,
    }


@shared_task(name="apps.network.tasks.measure_optical_signal_now")
def measure_optical_signal_now(
    organization_id: int,
    connection_ids: list[int] | None = None,
    outage_id: int | None = None,
) -> dict[str, Any]:
    """Ação manual "medir agora" — por cliente (`connection_ids`) ou por massiva.

    Existe como task porque a medição leva ~1,7 s por ONU e não pode segurar um
    request. A regra de acionamento continua intacta: isto só roda quando alguém
    pede explicitamente.
    """
    organization = Organization.objects.get(pk=organization_id)
    sources = registry.get_sources(organization, Capability.OPTICAL_SIGNAL)
    if not sources:
        return {"measured": 0, "reason": "no_sources"}

    token = set_current_organization(organization)
    try:
        if outage_id is not None:
            result = measure_now_for_outage(
                organization, sources[0], outage_id=outage_id
            )
        else:
            result = measure_now_for_connections(
                organization, sources[0], connection_ids=list(connection_ids or [])
            )
    except Exception as exc:
        _logger.error(
            "optical_signal_manual_measure_failed",
            organization=organization.slug,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return {"measured": 0, "reason": type(exc).__name__}
    finally:
        reset_current_organization(token)

    return {
        "measured": result.measured,
        "empty": result.empty,
        "skipped": result.skipped,
        "stopped_early": result.stopped_early,
    }


@shared_task(name="apps.network.tasks.dispatch_optical_signal_refresh_for_all_orgs")
def dispatch_optical_signal_refresh_for_all_orgs() -> dict[str, int]:
    """Beat diário: atualiza a linha de base de sinal de cada org habilitada."""
    return _dispatch_optical_refresh()


@allow_cross_tenant(reason="beat orchestrator itera OrganizationDataSource (não-TenantModel)")
def _dispatch_optical_refresh() -> dict[str, int]:
    from apps.tenancy.models import OrganizationDataSource

    configs = OrganizationDataSource.objects.filter(
        is_active=True,
        organization__is_active=True,
        capability=Capability.OPTICAL_SIGNAL.value,
    ).select_related("organization")

    orgs = {cfg.organization.pk: cfg.organization for cfg in configs}
    for org in orgs.values():
        refresh_optical_signal_baseline.apply_async(
            kwargs={"organization_id": org.pk}, queue=org.celery_queue_name
        )
    return {"orgs": len(orgs)}


@shared_task(name="apps.network.tasks.refresh_optical_signal_baseline")
def refresh_optical_signal_baseline(organization_id: int) -> dict[str, Any]:
    """Leitura passiva diária do sinal de todas as ONUs (uma listagem).

    Diária, e não a cada poll, porque **a coleta que a alimenta é diária**: o IXC
    varre as ONUs por volta das 06:30 e 4.551 das 4.554 leituras têm de 6 a 24
    horas. Puxar 4.559 linhas a cada 3 minutos reescreveria os mesmos valores.
    """
    organization = Organization.objects.get(pk=organization_id)
    sources = registry.get_sources(organization, Capability.OPTICAL_SIGNAL)
    if not sources:
        return {"updated": 0, "reason": "no_sources"}

    token = set_current_organization(organization)
    try:
        updated = refresh_optical_signals(organization, sources[0])
    except Exception as exc:
        _logger.error(
            "optical_signal_refresh_failed",
            organization=organization.slug,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
        return {"updated": 0, "reason": type(exc).__name__}
    finally:
        reset_current_organization(token)

    return {"updated": updated}
