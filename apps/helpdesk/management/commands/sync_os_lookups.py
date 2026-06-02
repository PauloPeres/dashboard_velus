"""Sincroniza nomes de assunto e técnico das OS IXC para o cache local.

Uso:
    python manage.py sync_os_lookups velus

O que faz:
  1. Busca `su_oss_assunto` (id → assunto) do IXC → subject_map
  2. Busca `funcionarios`   (id → funcionario) do IXC → technician_map
  3. Salva/atualiza OsLookupCache para a organização

Esses mapas resolvem os IDs opacos de `Ticket.subject_id` / `Ticket.technician_id`
em nomes legíveis nos dashboards de Ordens de Serviço.

Deve ser executado:
  - Na primeira instalação (junto do bootstrap de TICKETS)
  - Periodicamente (Celery Beat) — assuntos/técnicos mudam pouco
  - Após cadastrar novos assuntos ou técnicos no IXC
"""

from __future__ import annotations

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.integrations.ixc.client import IxcHttpClient
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Sincroniza su_oss_assunto + funcionarios do IXC → OsLookupCache"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")

    @allow_cross_tenant(reason="sync_os_lookups opera fora de request HTTP")
    def handle(self, *args: object, **opts: object) -> None:  # noqa: ARG002
        org_slug = str(opts["org_slug"])

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{org_slug}' não existe.") from exc

        set_current_organization(org)

        ds = OrganizationDataSource.objects.filter(
            organization=org,
            source_type=SourceType.IXC.value,
            capability=Capability.CUSTOMERS.value,
            is_active=True,
        ).first()
        if not ds:
            raise CommandError(
                f"Org '{org_slug}' sem credenciais IXC. "
                "Rode `setup_ixc_credentials` primeiro."
            )

        creds = ds.get_credentials()

        self.stdout.write(f"Sincronizando lookups de OS IXC para: {org_slug}")

        subject_map: dict[str, str] = {}
        technician_map: dict[str, str] = {}

        with IxcHttpClient(
            base_url=creds["base_url"],
            user_id=creds["user_id"],
            api_token=creds["api_token"],
        ) as client:

            # --- 1. su_oss_assunto (tipos de OS) ---
            self.stdout.write("  [1/2] Buscando su_oss_assunto (tipos de OS)...")
            for raw in client.paginate_ixc("su_oss_assunto", page_size=200):
                sid = str(raw.get("id", "")).strip()
                nome = str(raw.get("assunto", "")).strip()
                if sid and nome:
                    subject_map[sid] = nome
            self.stdout.write(f"     → {len(subject_map)} assuntos carregados")

            # --- 2. funcionarios (técnicos) ---
            self.stdout.write("  [2/2] Buscando funcionarios (técnicos)...")
            for raw in client.paginate_ixc("funcionarios", page_size=200):
                tid = str(raw.get("id", "")).strip()
                nome = str(raw.get("funcionario", "")).strip()
                if tid and nome:
                    technician_map[tid] = nome
            self.stdout.write(f"     → {len(technician_map)} técnicos carregados")

        from apps.helpdesk.infrastructure.models import OsLookupCache

        _cache, created = OsLookupCache.objects.update_or_create(
            organization=org,
            defaults={
                "subject_map": subject_map,
                "technician_map": technician_map,
                "synced_at": timezone.now(),
            },
        )
        action = "Criado" if created else "Atualizado"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ {action} OsLookupCache: "
                f"{len(subject_map)} assuntos, {len(technician_map)} técnicos"
            )
        )
        _logger.info(
            "sync_os_lookups_done",
            org=org_slug,
            subject_count=len(subject_map),
            technician_count=len(technician_map),
            created=created,
        )
