"""Bootstrap (carga total) de todas as capabilities ativas de uma org.

Uso:
    # Dispatcha pras filas Celery — não bloqueia, os workers processam:
    kubectl exec deploy/web -- python manage.py bootstrap_all velus

    # Roda inline no próprio processo — bloqueia até terminar, mostra contagens:
    python manage.py bootstrap_all velus --sync

Itera as OrganizationDataSource ATIVAS da org e roda `sync_capability` em modo
BOOTSTRAP (since=None → puxa o histórico completo) pra cada capability distinta.
Idempotente: as unique keys compostas no DB cuidam de rerun.

Ordem de execução segue a do enum Capability (Customers → Contracts → Invoices →
…), o que ajuda a resolver FKs já na primeira passada no modo inline. No modo
Celery as tasks rodam em paralelo na fila do tenant; FKs pendentes são resolvidas
nas passadas seguintes (modelo tolerante a ordem do sync).
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.shared.enums import Capability
from apps.shared.decorators import allow_cross_tenant
from apps.sync.models import SyncMode
from apps.sync.tasks import sync_capability
from apps.tenancy.models import Organization, OrganizationDataSource

# Ordem preferida = ordem de declaração do enum (Customers primeiro).
_CAP_ORDER = {cap.value: i for i, cap in enumerate(Capability)}


class Command(BaseCommand):
    help = "Bootstrap de todas as capabilities ativas de uma org."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da Organization existente")
        parser.add_argument(
            "--sync",
            action="store_true",
            help="Roda inline (bloqueia) em vez de dispatchar pra fila Celery.",
        )

    @allow_cross_tenant(
        reason="bootstrap_all opera fora de request HTTP, itera OrganizationDataSource"
    )
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug: str = opts["org_slug"]
        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization '{org_slug}' não existe.") from exc

        capabilities = sorted(
            OrganizationDataSource.objects.filter(organization=org, is_active=True)
            .values_list("capability", flat=True)
            .distinct(),
            key=lambda c: _CAP_ORDER.get(c, 999),
        )
        if not capabilities:
            raise CommandError(
                f"Org '{org_slug}' não tem nenhuma datasource ativa. "
                f"Rode `setup_ixc_credentials {org_slug}` antes."
            )

        run_inline: bool = opts.get("sync", False)
        mode = "inline" if run_inline else f"Celery → {org.celery_queue_name}"
        self.stdout.write(self.style.SUCCESS(
            f"\nBootstrap de '{org.slug}' — {len(capabilities)} capabilities ({mode}):\n"
        ))

        total = 0
        for cap in capabilities:
            if run_inline:
                result = sync_capability(
                    organization_id=org.pk,
                    capability=cap,
                    mode=SyncMode.BOOTSTRAP.value,
                )
                processed = result.get("records_processed", 0)
                total += processed
                self.stdout.write(f"  ✓ {cap}: {processed} registros")
            else:
                sync_capability.apply_async(
                    kwargs={
                        "organization_id": org.pk,
                        "capability": cap,
                        "mode": SyncMode.BOOTSTRAP.value,
                    },
                    queue=org.celery_queue_name,
                )
                self.stdout.write(f"  → {cap}: dispatchado")

        self.stdout.write("")
        if run_inline:
            self.stdout.write(self.style.SUCCESS(
                f"✓ Bootstrap concluído — {total} registros no total."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                "✓ Tasks dispatchadas. Acompanhe o progresso em /sync/ ou nos logs do worker."
            ))
