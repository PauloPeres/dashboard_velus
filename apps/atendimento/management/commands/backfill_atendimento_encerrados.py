"""Corrige o status das conversas que fecharam sem a gente ver.

O sync incremental do Opa! filtrava só por **data de abertura**, então conversa
aberta em maio e fechada em setembro nunca voltava na listagem: o status dela
ficava congelado como "em atendimento" no nosso banco.

*Medido em produção em 19/09/2026:* 813 conversas apareciam abertas, a mais
antiga de 20/05. Conferidas contra a API, **10 de 10 estavam CLOSED no Opa**.

O adapter já foi corrigido (passa a listar também por `dataInicialEncerramento`),
mas o que já está errado no banco não se conserta sozinho — o incremental olha
daqui para a frente. Este comando relista as conversas **encerradas desde uma
data** e reaplica o upsert.

Uso:
    python manage.py backfill_atendimento_encerrados velus --desde 2026-05-01 --dry-run
    python manage.py backfill_atendimento_encerrados velus --desde 2026-05-01
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.atendimento.infrastructure.models import Atendimento
from apps.atendimento.infrastructure.repositories import AtendimentoRepository
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Relista conversas encerradas no Opa! e corrige o status no banco."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str)
        parser.add_argument(
            "--desde",
            type=str,
            required=True,
            help="Data (AAAA-MM-DD) a partir da qual relistar encerramentos.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só relata quantas mudariam de status, sem gravar.",
        )

    @allow_cross_tenant(reason="backfill opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug = str(opts["org_slug"])
        dry_run = bool(opts["dry_run"])
        try:
            desde = timezone.make_aware(datetime.strptime(str(opts["desde"]), "%Y-%m-%d"))
        except ValueError as exc:
            raise CommandError("--desde precisa estar no formato AAAA-MM-DD.") from exc

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{org_slug}' não existe.") from exc
        set_current_organization(org)

        ds = OrganizationDataSource.objects.filter(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
            is_active=True,
        ).first()
        if ds is None:
            raise CommandError(f"Org '{org_slug}' sem credenciais Opa! ativas.")

        creds = ds.get_credentials()
        source = OpaAtendimentoSource(base_url=creds["base_url"], token=creds["token"])
        repo = AtendimentoRepository(org)

        abertos_antes = self._abertos(org)
        vistos = 0
        corrigidos = 0

        # Lista só por data de ENCERRAMENTO: é a passagem que faltava.
        # `_paginar` e o filtro de encerramento são internos ao adapter: este
        # comando é uma correção pontual do que já está no banco, não um caminho
        # de sync — e não vale alargar o port por causa dele.
        for dto in source._paginar(
            OpaAtendimentoSource._build_encerramento_filter(desde)
        ):
            vistos += 1
            atual = (
                Atendimento.objects.filter(organization=org, external_id=dto.external_id)
                .values_list("status", flat=True)
                .first()
            )
            if atual == dto.status:
                continue
            corrigidos += 1
            if not dry_run:
                repo.upsert_from_dto(dto, source_type=SourceType.OPA)

        abertos_depois = self._abertos(org)
        self.stdout.write(
            f"encerrados relistados: {vistos} · status divergente: {corrigidos}"
        )
        self.stdout.write(
            f"abertos no banco: {abertos_antes} → {abertos_depois}"
            + (" (dry-run, nada gravado)" if dry_run else "")
        )
        if not dry_run:
            _logger.info(
                "opa_backfill_encerrados",
                organization=org.slug,
                listados=vistos,
                corrigidos=corrigidos,
                abertos_antes=abertos_antes,
                abertos_depois=abertos_depois,
            )

    @staticmethod
    def _abertos(org: Organization) -> int:
        return Atendimento.objects.filter(
            organization=org,
            status__in=[Atendimento.Status.OPEN, Atendimento.Status.IN_PROGRESS],
        ).count()
