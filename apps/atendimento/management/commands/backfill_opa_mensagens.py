"""Ingere o volume de mensagens do Opa! Suite (metadado, sem texto).

Uso:
    python manage.py backfill_opa_mensagens velus --since 2026-01-30   # carga
    python manage.py backfill_opa_mensagens velus                      # incremental
    python manage.py backfill_opa_mensagens velus --max-pages 20 --dry-run
    python manage.py backfill_opa_mensagens velus --start-skip 2400000 # retomada

Sem `--since`, retoma do checkpoint `SyncCheckpoint(org, OPA, MENSAGENS)`; sem
checkpoint, cai em `--days` (default 30) pra nao disparar uma varredura de anos
por acidente.

O que ele NAO faz: guardar o texto das mensagens. Ver
`apps.atendimento.application.mensagens_backfill` pro porque.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.atendimento.application.mensagens_backfill import (
    MensagensBackfillResult,
    run_mensagens_backfill,
)
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncCheckpoint
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)

_DEFAULT_WINDOW_DAYS = 30


class Command(BaseCommand):
    help = "Ingere o volume de mensagens do Opa! Suite (metadado, sem texto)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help="Data inicial (YYYY-MM-DD). Ignora o checkpoint.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=_DEFAULT_WINDOW_DAYS,
            help=f"Janela em dias quando não há checkpoint. Default {_DEFAULT_WINDOW_DAYS}.",
        )
        parser.add_argument(
            "--start-skip",
            type=int,
            default=None,
            help="Retoma de um offset conhecido, pulando a busca binária da data.",
        )
        parser.add_argument(
            "--max-pages",
            type=int,
            default=None,
            help="Teto de páginas (100 mensagens cada) — para fatiar uma carga longa.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Não grava nem move o checkpoint; só mostra o que viria.",
        )

    @allow_cross_tenant(reason="backfill_opa_mensagens opera fora de request HTTP")
    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002
        org_slug: str = options["org_slug"]
        org = self._get_org(org_slug)
        set_current_organization(org)

        source = self._build_source(org)
        since, start_skip = self._resolve_cursor(org, options)

        if options["dry_run"]:
            self._dry_run(source, since, start_skip, options["max_pages"])
            return

        result = run_mensagens_backfill(
            org,
            source,
            since=since,
            start_skip=start_skip,
            max_pages=options["max_pages"],
            on_progress=lambda parcial: self._on_progress(org, parcial),
        )
        self._save_checkpoint(org, result)

        self.stdout.write(
            self.style.SUCCESS(
                f"{result.mensagens} mensagens · {result.paginas} páginas · "
                f"até {result.ultima_data or '—'}"
                + ("" if result.concluido else " · parou no teto de páginas")
            )
        )

    # -- Passos ---------------------------------------------------------------
    def _get_org(self, slug: str) -> Organization:
        org = Organization.objects.filter(slug=slug).first()
        if org is None:
            raise CommandError(f"Organização '{slug}' não encontrada.")
        return org

    def _build_source(self, org: Organization) -> OpaAtendimentoSource:
        ds = OrganizationDataSource.objects.filter(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
            is_active=True,
        ).first()
        if ds is None:
            raise CommandError(
                f"Org '{org.slug}' não tem datasource OPA/ATENDIMENTO ativo."
            )
        creds = ds.get_credentials()
        return OpaAtendimentoSource(base_url=creds["base_url"], token=creds["token"])

    def _resolve_cursor(
        self, org: Organization, options: dict[str, Any]
    ) -> tuple[datetime | None, int | None]:
        """Traduz as opções em (since, start_skip) — `--start-skip` vence."""
        start_skip: int | None = options["start_skip"]
        if start_skip is not None:
            return None, start_skip

        raw_since: str | None = options["since"]
        if raw_since:
            try:
                parsed = datetime.strptime(raw_since, "%Y-%m-%d")
            except ValueError as exc:
                raise CommandError("--since deve ser YYYY-MM-DD.") from exc
            return timezone.make_aware(parsed), None

        checkpoint = self._get_checkpoint(org)
        if checkpoint is not None and checkpoint.last_processed_at is not None:
            return checkpoint.last_processed_at, None

        return timezone.now() - timedelta(days=options["days"]), None

    def _dry_run(
        self,
        source: OpaAtendimentoSource,
        since: datetime | None,
        start_skip: int | None,
        max_pages: int | None,
    ) -> None:
        offset = (
            start_skip
            if start_skip is not None
            else (source.find_skip_for_date(since) if since else 0)
        )
        vistas = 0
        primeira: datetime | None = None
        ultima: datetime | None = None
        for _pos, dto in source.list_mensagens_global(
            start_skip=offset, max_pages=max_pages or 1
        ):
            vistas += 1
            if dto.sent_at is not None:
                primeira = primeira or dto.sent_at
                ultima = dto.sent_at
        self.stdout.write(
            f"[dry-run] offset {offset} · {vistas} mensagens · "
            f"{primeira or '—'} → {ultima or '—'} (nada gravado)"
        )

    def _on_progress(self, org: Organization, parcial: MensagensBackfillResult) -> None:
        """Persiste o cursor no meio do caminho e mostra andamento.

        Uma carga inicial leva horas; sem isso, uma queda de conexão na terceira
        hora custaria as três.
        """
        self._save_checkpoint(org, parcial)
        self.stdout.write(
            f"  … {parcial.mensagens} mensagens (offset {parcial.ultimo_offset}, "
            f"até {parcial.ultima_data or '—'})"
        )

    def _get_checkpoint(self, org: Organization) -> SyncCheckpoint | None:
        return SyncCheckpoint.objects.filter(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.MENSAGENS.value,
        ).first()

    def _save_checkpoint(
        self, org: Organization, result: MensagensBackfillResult
    ) -> None:
        """Move o cursor só quando houve mensagem — rodada vazia não avança.

        Mesma lição do #132: um checkpoint que anda sozinho em rodada vazia
        transforma uma fonte quebrada em silêncio.
        """
        if result.ultima_data is None:
            return
        SyncCheckpoint.objects.update_or_create(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.MENSAGENS.value,
            defaults={
                "last_processed_at": result.ultima_data,
                "consecutive_empty_runs": 0,
            },
        )
