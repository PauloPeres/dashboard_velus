"""Management command: backfill_supplier_names

Busca todos os fornecedores na API IXC e atualiza o campo `supplier_name`
em todas as despesas onde o nome está armazenado como `Fornecedor #XXX`
(o fallback antigo que usava só `fantasia`, ignorando `razao social`).

Uso:
    uv run python manage.py backfill_supplier_names
    uv run python manage.py backfill_supplier_names --dry-run
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.integrations.ixc.expenses import IxcSupplierCache
from apps.integrations.ixc.client import IxcHttpClient
from apps.tenancy.models import Organization, OrganizationDataSource
from apps.financial.infrastructure.models import Expense
from apps.shared.context import set_current_organization


class Command(BaseCommand):
    help = "Atualiza supplier_name nas despesas onde está armazenado como 'Fornecedor #XXX'"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Apenas mostra o que seria atualizado sem gravar no banco",
        )
        parser.add_argument(
            "--org",
            default=None,
            help="Nome da organização (default: primeira com datasource IXC ativo)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        org_name = options["org"]

        # Get org + IXC datasource
        if org_name:
            org = Organization.objects.get(name=org_name)
        else:
            ds_qs = OrganizationDataSource.objects.filter(
                source_type="IXC", is_active=True
            ).select_related("organization")
            ds_first = ds_qs.first()
            org = ds_first.organization if ds_first else None

        if not org:
            self.stderr.write("Nenhuma organização com IXC ativo encontrada.")
            return

        ds = OrganizationDataSource.objects.filter(
            organization=org, source_type="IXC", is_active=True,
        ).first()
        creds = ds.get_credentials()

        self.stdout.write(f"Organização: {org.name}")
        self.stdout.write("Carregando fornecedores da API IXC...")

        # Load full supplier map using the fixed cache (fantasia or razao)
        with IxcHttpClient(**creds) as client:
            cache = IxcSupplierCache(client)
            # Trigger load
            cache.get_name("0")
            supplier_map = cache._suppliers

        self.stdout.write(f"  → {len(supplier_map)} fornecedores carregados")

        # Set org context
        set_current_organization(org)

        # Find all expenses with anonymized names
        anon_expenses = Expense.objects.filter(
            supplier_name__startswith="Fornecedor #"
        )
        total_anon = anon_expenses.count()
        self.stdout.write(f"  → {total_anon} despesas com 'Fornecedor #XXX' no banco")

        if total_anon == 0:
            self.stdout.write(self.style.SUCCESS("Nada a atualizar."))
            return

        updated = 0
        skipped = 0

        for expense in anon_expenses.iterator():
            sid = expense.supplier_external_id
            real_name = supplier_map.get(str(sid), "")

            # Only update if we found a non-anonymous name
            if real_name and not real_name.startswith("Fornecedor #"):
                if not dry_run:
                    Expense.objects.filter(pk=expense.pk).update(supplier_name=real_name)
                self.stdout.write(
                    f"  {'[DRY-RUN] ' if dry_run else ''}id={expense.external_id} "
                    f"'{expense.supplier_name}' → '{real_name}'"
                )
                updated += 1
            else:
                skipped += 1

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"\n[DRY-RUN] Seriam atualizadas: {updated}, sem nome real: {skipped}"
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"\nAtualizadas: {updated}, sem nome real na API: {skipped}"
            ))
