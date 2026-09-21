"""Tira da fila de causa as massivas que ninguém vai conseguir responder.

Decisão do operador em 21/09/2026: a fila tinha 56 massivas, quase todas de
antes de o campo existir. Ninguém lembra o que foi um evento de 09/09, e **boa
parte nem tem nome** — são clusters geográficos, sem elemento de rede. Chute
vira rótulo errado no treino, que é pior que rótulo nenhum.

Elas saem da fila **dispensadas**, não respondidas. A diferença é o ponto: uma
massiva sem causa e uma massiva descartada são coisas distintas, e o dia em que
o modelo for treinado é o dia em que isso importa.

Uso:
    python manage.py dispensar_causa_antigas velus --ate 2026-09-21 --dry-run
    python manage.py dispensar_causa_antigas velus --ate 2026-09-21
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.network.infrastructure.models import OutageEvent
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

MOTIVO_PADRAO = (
    "Evento anterior à fila de causa — ninguém lembraria o que foi, e chute "
    "vira rótulo errado no treino."
)


class Command(BaseCommand):
    help = "Dispensa da fila de causa as massivas encerradas até uma data."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str)
        parser.add_argument(
            "--ate",
            type=str,
            required=True,
            help="Dispensa o que encerrou ATÉ esta data (AAAA-MM-DD), inclusive.",
        )
        parser.add_argument("--motivo", type=str, default=MOTIVO_PADRAO)
        parser.add_argument("--dry-run", action="store_true")

    @allow_cross_tenant(reason="operação administrativa fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        try:
            limite = timezone.make_aware(
                datetime.strptime(str(opts["ate"]), "%Y-%m-%d")
            ).replace(hour=23, minute=59, second=59)
        except ValueError as exc:
            raise CommandError("--ate precisa estar no formato AAAA-MM-DD.") from exc

        try:
            org = Organization.objects.get(slug=str(opts["org_slug"]))
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{opts['org_slug']}' não existe.") from exc
        set_current_organization(org)

        alvo = OutageEvent.objects.filter(
            organization=org,
            ended_at__isnull=False,
            ended_at__lte=limite,
            confirmed_cause="",
            cause_waived_at__isnull=True,
        )
        total = alvo.count()
        sem_nome = alvo.filter(element_external_id="").count()

        self.stdout.write(
            f"{total} massiva(s) encerrada(s) até {limite:%d/%m/%Y} sem causa "
            f"— {sem_nome} delas sem elemento de rede (cluster geográfico)."
        )
        if opts["dry_run"]:
            self.stdout.write("[dry-run] nada foi gravado.")
            return

        # `update` direto: são dezenas de linhas e o que se grava é o mesmo
        # carimbo para todas — instanciar objeto a objeto só gastaria.
        atualizadas = alvo.update(
            cause_waived_at=timezone.now(),
            cause_waived_reason=str(opts["motivo"])[:255],
        )
        _logger.info(
            "outage_cause_waived",
            organization=org.slug,
            total=atualizadas,
            ate=str(limite.date()),
        )
        self.stdout.write(f"{atualizadas} massiva(s) dispensada(s) da fila.")
