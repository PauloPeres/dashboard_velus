"""Corrige `OutageEvent.affected_fraction` gravado acima de 100%.

Em produção (2026-09-18) quatro massivas tinham fração impossível: PON 364 com
2,25, PON 385 com 1,29 e dois clusters GEO com 1,14. A causa era o denominador,
não a escala — numerador e denominador falavam de populações diferentes (ver
`apps.network.domain.outage._affected_for_fraction`). O detector já foi
corrigido; este comando é só para os registros que ficaram no banco, porque a
massiva encerrada não é recalculada por ninguém: ela é o registro que dura.

O recálculo é **aproximado e assumido como tal**: usa o cadastro de HOJE e os
afetados gravados, enquanto o valor original era o pico medido durante o evento.
Por isso o comando não toca em fração ≤ 1 — ali o número antigo, ainda que de
outro momento, não é falso.

Uso:
    python manage.py fix_outage_fractions velus --dry-run
    python manage.py fix_outage_fractions velus
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.customers.infrastructure.models import Contract
from apps.network.infrastructure.models import (
    Connection,
    OutageAffectedLogin,
    OutageEvent,
)
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Recalcula affected_fraction das massivas com fração acima de 100%."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só relata o que mudaria, sem gravar.",
        )

    @allow_cross_tenant(reason="correção de dado opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug = str(opts["org_slug"])
        dry_run = bool(opts["dry_run"])

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{org_slug}' não existe.") from exc
        set_current_organization(org)

        quebradas = list(
            OutageEvent.objects.filter(
                organization=org, affected_fraction__gt=1.0
            ).order_by("started_at")
        )
        if not quebradas:
            self.stdout.write("Nenhuma massiva com fração acima de 100%.")
            return

        inativos = set(
            Contract.objects.filter(organization=org)
            .exclude(status=Contract.Status.ACTIVE)
            .values_list("external_id", flat=True)
        )
        por_pon: dict[str, int] = defaultdict(int)
        por_cto: dict[str, int] = defaultdict(int)
        rows = Connection.objects.filter(organization=org).values_list(
            "pon_external_id", "cto_external_id", "contract_external_id"
        )
        for pon_id, cto_id, contract_id in rows:
            if contract_id and contract_id in inativos:
                continue
            if pon_id:
                por_pon[pon_id] += 1
            if cto_id:
                por_cto[cto_id] += 1

        atualizadas = 0
        for outage in quebradas:
            nova = self._recalcular(org, outage, por_pon=por_pon, por_cto=por_cto)
            self.stdout.write(
                f"massiva {outage.pk} ({outage.scope} {outage.element_external_id or '—'}): "
                f"{outage.affected_fraction:.2f} → "
                f"{'—' if nova is None else f'{nova:.2f}'}"
            )
            if nova is None or dry_run:
                continue
            outage.affected_fraction = nova
            outage.save(update_fields=["affected_fraction", "updated_at"])
            atualizadas += 1
            _logger.info(
                "outage_fraction_fixed",
                outage=outage.pk,
                scope=outage.scope,
                element=outage.element_external_id,
                fraction=nova,
            )

        if dry_run:
            self.stdout.write(f"[dry-run] {len(quebradas)} massiva(s) seriam revistas.")
        else:
            self.stdout.write(f"{atualizadas} massiva(s) corrigida(s).")

    def _recalcular(
        self,
        org: Organization,
        outage: OutageEvent,
        *,
        por_pon: dict[str, int],
        por_cto: dict[str, int],
    ) -> float | None:
        """Fração pelo mesmo critério do detector corrigido, ou None se não dá.

        Devolver None é resposta legítima: quando não há denominador no cadastro
        de hoje, escrever qualquer número seria trocar um valor errado conhecido
        por um inventado.
        """
        afetados = list(
            OutageAffectedLogin.objects.filter(organization=org, outage=outage)
            .select_related("drop_event", "drop_event__connection")
        )
        if not afetados:
            return None

        if outage.scope == OutageEvent.Scope.PON:
            porta = outage.element_external_id
            denominador = por_pon.get(porta, 0)
            numerador = sum(
                1
                for a in afetados
                if a.drop_event
                and (
                    getattr(a.drop_event.connection, "pon_external_id", "") or ""
                ).strip()
                == porta
            )
        else:
            # GEO e o resto: o denominador possível é o das caixas envolvidas, e
            # o numerador só pode ter quem está numa delas.
            ctos = {
                a.drop_event.cto_external_id
                for a in afetados
                if a.drop_event and a.drop_event.cto_external_id
            }
            denominador = sum(por_cto.get(cto, 0) for cto in ctos)
            numerador = sum(
                1 for a in afetados if a.drop_event and a.drop_event.cto_external_id
            )

        if not denominador or not numerador:
            return None
        return min(numerador / denominador, 1.0)
