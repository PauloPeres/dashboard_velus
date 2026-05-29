"""Sincroniza o plano de contas IXC para o cache local (PlanoContasCache).

Uso:
    python manage.py sync_planejamento velus

O que faz:
  1. Busca todos os registros de `planejamento` (≈91) do IXC → plano_map
  2. Busca todos os registros de `planejamento_analitico` do IXC → conta_map
  3. Salva/atualiza PlanoContasCache para a organização

Deve ser executado:
  - Na primeira instalação
  - Periodicamente (Celery Beat, ex: diário)
  - Após criar novos planos analíticos no IXC
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
    help = "Sincroniza planejamento + planejamento_analitico do IXC → PlanoContasCache"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")

    @allow_cross_tenant(reason="sync_planejamento opera fora de request HTTP")
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

        self.stdout.write(f"Sincronizando plano de contas IXC para: {org_slug}")

        plano_map: dict[str, dict] = {}
        conta_map: dict[str, str] = {}

        with IxcHttpClient(
            base_url=creds["base_url"],
            user_id=creds["user_id"],
            api_token=creds["api_token"],
        ) as client:

            # --- 1. planejamento (categorias pai, ≈91 registros) ---
            self.stdout.write("  [1/2] Buscando planejamento (categorias pai)...")
            count_plano = 0
            for raw in client.paginate_ixc("planejamento", page_size=200):
                id_plano = str(raw.get("id", "")).strip()
                if not id_plano:
                    continue
                plano_map[id_plano] = {
                    "cod":  raw.get("cod_planejamento", "").strip(),
                    "nome": raw.get("planejamento", "").strip(),
                    "tipo": raw.get("tipo", "").strip(),
                }
                count_plano += 1
            self.stdout.write(f"     → {count_plano} categorias carregadas")

            # --- 2. planejamento_analitico (contas detalhadas, ≈11k registros) ---
            self.stdout.write("  [2/2] Buscando planejamento_analitico (contas analíticas)...")
            count_conta = 0
            for raw in client.paginate_ixc("planejamento_analitico", page_size=500):
                id_conta = str(raw.get("id", "")).strip()
                id_plano = str(raw.get("id_planejamento", "0")).strip()
                if not id_conta:
                    continue
                conta_map[id_conta] = id_plano
                count_conta += 1
            self.stdout.write(f"     → {count_conta} contas analíticas carregadas")

        # Garante entrada "0" para fallback
        plano_map["0"] = {"cod": "", "nome": "(Sem categoria)", "tipo": "?"}
        conta_map["0"] = "0"

        # Salva no DB
        from apps.analytics.infrastructure.models import PlanoContasCache

        cache, created = PlanoContasCache.objects.update_or_create(
            organization=org,
            defaults={
                "plano_map": plano_map,
                "conta_map": conta_map,
                "synced_at": timezone.now(),
            },
        )
        action = "Criado" if created else "Atualizado"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n✓ {action} PlanoContasCache: "
                f"{count_plano} categorias, {count_conta} contas analíticas"
            )
        )
        _logger.info(
            "sync_planejamento_done",
            org=org_slug,
            plano_count=count_plano,
            conta_count=count_conta,
            created=created,
        )
