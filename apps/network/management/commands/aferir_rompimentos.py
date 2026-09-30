"""Refaz a aferição das massivas com rompimento registrado, com o algoritmo de hoje.

É o backtest contínuo pedido em 30/09/2026 (massiva 359): cada massiva com o
ponto do rompimento registrado é um caso de teste. Mudou a regra da rota? Rode
isto antes de subir e compare o erro de antes com o de agora, massiva por
massiva — uma regra que acerta a massiva nova e erra três antigas não é melhor.

A conta usa a **evidência guardada** no registro (quem caiu, quem estava
online), não a de hoje: quem está online hoje não é quem estava online no dia,
e refazer com a evidência de hoje mediria outra coisa.

    python manage.py aferir_rompimentos --org velus
    python manage.py aferir_rompimentos --org velus --gravar
"""

from __future__ import annotations

from statistics import median
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.dashboards.massivas import RAIO_ACERTO_METROS, aferir_rompimento, quedas_da_massiva
from apps.network.infrastructure.models import OutageEvent
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization


class Command(BaseCommand):
    help = "Refaz a aferição das massivas com rompimento registrado, com o algoritmo de hoje."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--org", required=True, help="slug da organização")
        parser.add_argument(
            "--gravar",
            action="store_true",
            help="grava a aferição nova no lugar da antiga (sem isto, só compara)",
        )

    @allow_cross_tenant(reason="backtest opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002 — assinatura do Django
        try:
            org = Organization.objects.get(slug=opts["org"])
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{opts['org']}' não existe.") from exc
        set_current_organization(org)

        massivas = list(
            OutageEvent.objects.filter(
                organization=org,
                break_latitude__isnull=False,
                break_longitude__isnull=False,
            ).order_by("started_at")
        )
        if not massivas:
            self.stdout.write("Nenhuma massiva com rompimento registrado.")
            return

        erros_agora: list[int] = []
        acertos = 0
        for outage in massivas:
            antes = outage.break_evaluation or {}
            evidencia = antes.get("evidencia") or {}
            ponto = (float(outage.break_latitude or 0), float(outage.break_longitude or 0))
            agora = aferir_rompimento(
                org,
                quedas_da_massiva(org, outage),
                ponto,
                ctos_fora=set(evidencia["fora"]) if "fora" in evidencia else None,
                ctos_no_ar=set(evidencia["no_ar"]) if "no_ar" in evidencia else None,
            )
            erro_antes = (antes.get("erros_m") or {}).get("hipotese_1")
            erro_agora = agora["erros_m"].get("hipotese_1")
            if erro_agora is not None:
                erros_agora.append(erro_agora)
                acertos += erro_agora <= RAIO_ACERTO_METROS
            self.stdout.write(
                f"massiva {outage.pk} ({outage.started_at:%d/%m/%Y %H:%M}, "
                f"{outage.affected_count} clientes): 1ª hipótese "
                f"{_m(erro_antes)} [{antes.get('algoritmo', '—')}] → "
                f"{_m(erro_agora)} [{agora['algoritmo']}] · trecho certo em "
                f"{agora['posicao_do_trecho_certo'] or '—'}º"
            )
            if opts["gravar"]:
                outage.break_evaluation = agora
                outage.save(update_fields=["break_evaluation", "updated_at"])

        if erros_agora:
            self.stdout.write(
                f"{len(massivas)} massiva(s) · acerto do trecho (≤{RAIO_ACERTO_METROS:.0f} m): "
                f"{acertos}/{len(erros_agora)} · mediana do erro {median(erros_agora):.0f} m"
            )
        if not opts["gravar"]:
            self.stdout.write("(só comparação — use --gravar para substituir as aferições)")


def _m(valor: int | None) -> str:
    return "—" if valor is None else f"{valor} m"
