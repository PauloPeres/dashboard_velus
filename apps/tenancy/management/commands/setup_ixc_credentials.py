"""Setup interativo de credenciais IXC pra uma org — seguro (token escondido).

Uso:
    docker compose exec web python manage.py setup_ixc_credentials velus

Vai pedir:
- Base URL (ex: https://erp.empresa.com.br)
- User ID
- API Token (input escondido via getpass)

Cria/atualiza OrganizationDataSource pra TODAS as capabilities suportadas pelo
adapter IXC (Customers, Contracts, Invoices). Credenciais criptografadas com
Fernet antes de salvar no DB.

Token NUNCA aparece em log, stdout, ou stderr — só em memória do processo.
"""

from __future__ import annotations

import getpass
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource


class Command(BaseCommand):
    help = "Configura credenciais IXC pra uma org (interativo, token oculto)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da Organization existente")
        parser.add_argument(
            "--non-interactive",
            action="store_true",
            help="Lê de env vars IXC_BASE_URL / IXC_USER_ID / IXC_API_TOKEN (use só em CI).",
        )

    @allow_cross_tenant(reason="setup_ixc_credentials opera fora de request HTTP")
    def handle(self, *args: Any, **opts: Any) -> None:  # noqa: ARG002
        org_slug: str = opts["org_slug"]

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(
                f"Organization '{org_slug}' não existe. "
                f"Rode `create_organization {org_slug} ...` antes."
            ) from exc

        set_current_organization(org)
        self.stdout.write(self.style.SUCCESS(f"\nConfigurando IXC para '{org.slug}'\n"))

        if opts.get("non_interactive"):
            import os
            base_url = os.environ.get("IXC_BASE_URL", "").strip().rstrip("/")
            user_id = os.environ.get("IXC_USER_ID", "").strip()
            api_token = os.environ.get("IXC_API_TOKEN", "").strip()
            if not all([base_url, user_id, api_token]):
                raise CommandError(
                    "Modo non-interactive exige IXC_BASE_URL, IXC_USER_ID, IXC_API_TOKEN no env."
                )
        else:
            base_url = input("Base URL do IXC (ex: https://erp.empresa.com.br): ").strip().rstrip("/")
            user_id = input("User ID (numérico): ").strip()
            # getpass: input invisível, não vai pro histórico do shell
            api_token = getpass.getpass("API Token (não aparece na tela): ").strip()

        if not base_url.startswith(("http://", "https://")):
            raise CommandError("Base URL precisa começar com http:// ou https://")
        if not user_id:
            raise CommandError("User ID não pode ser vazio.")
        if not api_token:
            raise CommandError("API Token não pode ser vazio.")

        credentials = {
            "base_url": base_url,
            "user_id": user_id,
            "api_token": api_token,
        }

        # Cria/atualiza datasource pra cada capability suportada pelo IXC adapter
        ixc_capabilities = [
            Capability.CUSTOMERS,
            Capability.CONTRACTS,
            Capability.INVOICES,
        ]

        created_count = 0
        updated_count = 0
        for cap in ixc_capabilities:
            ds, created = OrganizationDataSource.objects.get_or_create(
                organization=org,
                source_type=SourceType.IXC.value,
                capability=cap.value,
                defaults={"priority": 100, "is_active": True},
            )
            ds.set_credentials(credentials)
            ds.is_active = True
            ds.save()
            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"  ✓ Criado: IXC/{cap.value}"))
            else:
                updated_count += 1
                self.stdout.write(f"  ↻ Atualizado: IXC/{cap.value}")

        # Mascarar token nas mensagens — só mostra prefixo/sufixo
        masked_token = (
            f"{api_token[:3]}…{api_token[-3:]}" if len(api_token) > 8 else "***"
        )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Resumo:"))
        self.stdout.write(f"  Base URL:     {base_url}")
        self.stdout.write(f"  User ID:      {user_id}")
        self.stdout.write(f"  Token (mask): {masked_token}")
        self.stdout.write(f"  DataSources:  {created_count} criadas, {updated_count} atualizadas")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "✓ Credenciais criptografadas com Fernet no DB.\n"
            "  Próximos passos:\n"
            "    1. Teste 1 endpoint: python manage.py ixc_explore velus --endpoint=cliente --limit=3\n"
            "    2. Bootstrap completo: abra /sync/ e clique 'Bootstrap' em cada linha\n"
        ))
