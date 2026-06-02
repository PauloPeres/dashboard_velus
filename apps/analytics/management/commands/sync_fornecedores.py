"""Sincroniza os fornecedores IXC para o cache local (FornecedorCache).

Uso:
    python manage.py sync_fornecedores velus

O que faz:
  1. Busca todos os registros de `fornecedor` do IXC (fantasia → razao social)
  2. Salva/atualiza FornecedorCache para a organização

O DRE-Contas resolve `Expense.supplier_external_id → nome` a partir desse
cache em tempo de exibição, inclusive para despesas gravadas com o fallback
antigo `Fornecedor #X`.
"""

from __future__ import annotations

import structlog
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.utils import timezone

from apps.integrations.ixc.client import IxcHttpClient
from apps.integrations.ixc.expenses import IxcSupplierCache
from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource

_logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Sincroniza fornecedores do IXC → FornecedorCache"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")

    @allow_cross_tenant(reason="sync_fornecedores opera fora de request HTTP")
    def handle(self, *args, **opts) -> None:
        org_slug: str = opts["org_slug"]

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{org_slug}' não existe.") from exc

        set_current_organization(org)

        ds = (
            OrganizationDataSource.objects.filter(
                organization=org,
                source_type=SourceType.IXC.value,
                capability=Capability.CUSTOMERS.value,
                is_active=True,
            ).first()
        )
        if not ds:
            raise CommandError(
                f"Org '{org_slug}' sem credenciais IXC. "
                "Rode `setup_ixc_credentials` primeiro."
            )

        creds = ds.get_credentials()

        self.stdout.write(f"Sincronizando fornecedores IXC para: {org_slug}")

        with IxcHttpClient(
            base_url=creds["base_url"],
            user_id=creds["user_id"],
            api_token=creds["api_token"],
        ) as client:
            supplier_map = IxcSupplierCache(client).load_all()

        from apps.analytics.infrastructure.models import FornecedorCache

        _, created = FornecedorCache.objects.update_or_create(
            organization=org,
            defaults={
                "supplier_map": supplier_map,
                "synced_at": timezone.now(),
            },
        )
        action = "Criado" if created else "Atualizado"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ {action} FornecedorCache: {len(supplier_map)} fornecedores"
            )
        )
        _logger.info(
            "sync_fornecedores_done",
            org=org_slug,
            supplier_count=len(supplier_map),
            created=created,
        )
