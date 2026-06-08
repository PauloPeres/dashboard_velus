"""Sincroniza atendimentos/conversas do Opa! Suite para o dashboard (read-only).

Uso:
    python manage.py sync_opasuite velus               # incremental (90 dias)
    python manage.py sync_opasuite velus --full        # ignora checkpoint
    python manage.py sync_opasuite velus --days 180     # janela custom
    python manage.py sync_opasuite velus --with-messages --message-limit 200

O que faz (ver `apps.atendimento.application.sync.run_opa_sync`):
  1. Sincroniza departamentos.
  2. Monta o mapa cliente_opaco -> CPF/CNPJ (liga conversa -> Customer).
  3. Sincroniza atendimentos da janela.
  4. (Opcional) ingere mensagens — 1 chamada por atendimento, caro.

Janela default = 90 dias (carga inicial 3-6 meses, decisao do escopo). O cursor
incremental fica em SyncCheckpoint(org, OPA, ATENDIMENTO); `--full` ignora.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.atendimento.application.sync import run_opa_sync
from apps.integrations.opa.atendimento import OpaAtendimentoSource
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncCheckpoint
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Sincroniza atendimentos do Opa! Suite (read-only) para uma org."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")
        parser.add_argument(
            "--days",
            type=int,
            default=90,
            help="Janela de carga (dias) quando não há checkpoint. Default 90.",
        )
        parser.add_argument(
            "--full",
            action="store_true",
            help="Ignora o checkpoint e recarrega a janela inteira.",
        )
        parser.add_argument(
            "--with-messages",
            action="store_true",
            help="Também ingere as mensagens (1 chamada por atendimento — caro).",
        )
        parser.add_argument(
            "--message-limit",
            type=int,
            default=None,
            help="Máx. de atendimentos com mensagens ingeridas (controle de custo).",
        )

    @allow_cross_tenant(reason="sync_opasuite opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug = str(opts["org_slug"])
        days = int(opts["days"])
        full = bool(opts["full"])
        with_messages = bool(opts["with_messages"])
        message_limit = opts["message_limit"]

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
        if not ds:
            raise CommandError(
                f"Org '{org_slug}' sem credenciais Opa!. "
                "Rode `setup_opa_credentials` primeiro."
            )

        creds = ds.get_credentials()

        checkpoint, _ = SyncCheckpoint.objects.get_or_create(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
        )

        if full:
            since = timezone.now() - timedelta(days=days)
        elif checkpoint.last_processed_at:
            since = checkpoint.last_processed_at
        else:
            since = timezone.now() - timedelta(days=days)

        self.stdout.write(
            f"Sincronizando Opa! Suite para '{org_slug}' desde {since:%Y-%m-%d}"
            + (" (com mensagens)" if with_messages else "")
        )

        source = OpaAtendimentoSource(
            base_url=creds["base_url"],
            token=creds["token"],
        )

        started_at = timezone.now()
        result = run_opa_sync(
            org,
            source,
            since=since,
            with_messages=with_messages,
            message_limit=message_limit,
        )

        checkpoint.last_processed_at = started_at
        checkpoint.save(update_fields=["last_processed_at", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ Opa! sync: {result.atendimentos} atendimentos "
                f"({result.customers_linked} ligados a cliente), "
                f"{result.departamentos} departamentos, "
                f"{result.mensagens} mensagens"
            )
        )
