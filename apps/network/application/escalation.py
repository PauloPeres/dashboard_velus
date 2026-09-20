"""Escalonamento de massiva crítica para o Telegram (P7 do painel de NOC).

A regra, em uma frase: **a TV é consciência contínua; o push exige ação de
alguém.** Com plantão 24/7 na sala, o Telegram deixa de ser o canal principal e
vira escalonamento — o que alguém precisa largar o que está fazendo para
atender, e nada além disso.

Quatro filtros, e cada um existe para o alerta continuar funcionando depois do
primeiro mês:

1. **só CRÍTICO.** O que está na lista fica na lista;
2. **só o que ninguém assumiu.** Reconhecimento (P8) é justamente a mensagem
   "estou tratando" — insistir depois dela é transformar aviso em barulho;
3. **só depois de N minutos.** Primeiro a TV mostra; o celular toca se o evento
   sobreviver ao tempo em que alguém normalmente já teria pegado;
4. **uma vez por evento.** Massiva que cresce não manda mensagem de novo. Sem
   isso, o loop de 5 min viraria uma mensagem a cada 5 min — que é como se
   desliga um canal de alerta.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from django.conf import settings
from django.utils import timezone

from apps.network.infrastructure.models import OutageEvent

_logger = structlog.get_logger(__name__)

# Escopos que, sozinhos, já são infraestrutura fora do ar. Espelha
# `panels.alerts.ESCOPOS_CRITICOS` — se um dia divergirem, a TV e o push vão
# discordar sobre o que é grave, e a sala vai acreditar no que gritar mais alto.
ESCOPOS_CRITICOS = {
    OutageEvent.Scope.OLT,
    OutageEvent.Scope.POP,
}


def massivas_para_escalar(
    organization: Any, *, now: datetime | None = None
) -> list[OutageEvent]:
    """Massivas abertas que merecem um push agora."""
    now = now or timezone.now()
    minutos = int(getattr(settings, "TELEGRAM_ESCALATION_MINUTES", 15))
    corte = now - timedelta(minutes=minutos)

    return list(
        OutageEvent.objects.filter(
            organization=organization,
            ended_at__isnull=True,
            acknowledged_at__isnull=True,
            escalated_at__isnull=True,
            started_at__lte=corte,
            scope__in=ESCOPOS_CRITICOS,
        )
        # Manutenção programada avisada não vira push: o evento é esperado, e
        # acordar alguém por causa dele é o caminho mais curto para a equipe
        # silenciar o canal.
        .filter(maintenance_event_id__isnull=True)
        .order_by("-affected_count")
    )


def mensagem_de(outage: OutageEvent) -> str:
    """O texto do push — curto, porque é lido no celular, às vezes de madrugada.

    Diz **o que**, **onde**, **quanto** e **há quanto tempo**; o resto está na
    tela. Não promete causa: o veredito automático pode estar errado, e no push
    não há espaço para a ressalva que a tela sempre carrega.
    """
    minutos = int((timezone.now() - outage.started_at).total_seconds() // 60)
    onde = outage.suspected_segment_label or outage.element_label or "elemento não identificado"
    ainda_fora = max((outage.affected_count or 0) - (outage.restored_count or 0), 0)
    return (
        f"🔴 <b>Massiva sem ninguém tratando</b>\n"
        f"{onde}\n"
        f"{ainda_fora} de {outage.affected_count} clientes ainda fora · "
        f"há {minutos} min\n"
        f"Escopo {outage.get_scope_display()}"
    )


def escalar_massivas(organization: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Manda o push das massivas que se qualificam e carimba o envio.

    O carimbo só é gravado quando a mensagem **saiu**: se o Telegram estiver
    desligado ou falhar, o evento continua elegível, e o próximo ciclo tenta de
    novo. Carimbar mesmo assim seria registrar um aviso que ninguém recebeu.
    """
    now = now or timezone.now()
    candidatas = massivas_para_escalar(organization, now=now)
    if not candidatas:
        return {"candidatas": 0, "enviadas": 0}

    from apps.shared.notifications.telegram import enviar_telegram

    enviadas = 0
    for outage in candidatas:
        saiu = enviar_telegram(
            mensagem_de(outage),
            contexto={"outage": outage.pk, "organization": getattr(organization, "slug", "")},
        )
        if not saiu:
            continue
        OutageEvent.objects.filter(pk=outage.pk).update(escalated_at=now)
        enviadas += 1
        _logger.info("outage_escalated", outage=outage.pk, scope=outage.scope)

    return {"candidatas": len(candidatas), "enviadas": enviadas}
