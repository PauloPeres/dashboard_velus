"""Setup de credenciais Opa! Suite pra uma org (token oculto/criptografado).

Uso:
    # Interativo (token oculto via getpass):
    python manage.py setup_opa_credentials velus

    # A partir das settings (OPA_LINK / OPA_TOKEN do .env / Secret):
    python manage.py setup_opa_credentials velus --from-settings

Cria/atualiza OrganizationDataSource (OPA / ATENDIMENTO) com credenciais
criptografadas (Fernet). O Opa! e read-only no Velus — so consumimos GETs.

Token NUNCA aparece em log/stdout — só em memória.
"""

from __future__ import annotations

import getpass
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.context import set_current_organization
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization, OrganizationDataSource


class Command(BaseCommand):
    help = "Configura credenciais Opa! Suite pra uma org (token oculto)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da Organization existente")
        parser.add_argument(
            "--from-settings",
            action="store_true",
            help="Lê base_url/token de settings.OPA_LINK / settings.OPA_TOKEN.",
        )

    @allow_cross_tenant(reason="setup_opa_credentials opera fora de request HTTP")
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
        self.stdout.write(self.style.SUCCESS(f"\nConfigurando Opa! para '{org.slug}'\n"))

        if opts.get("from_settings"):
            base_url = str(getattr(settings, "OPA_LINK", "")).strip().rstrip("/")
            token = str(getattr(settings, "OPA_TOKEN", "")).strip()
            if not base_url or not token:
                raise CommandError(
                    "OPA_LINK / OPA_TOKEN não configurados nas settings (.env)."
                )
            self.stdout.write("  Lendo credenciais de settings (OPA_LINK / OPA_TOKEN).")
        else:
            base_url = input(
                "Base URL do Opa! (ex: https://opasuite.empresa.net.br): "
            ).strip().rstrip("/")
            token = getpass.getpass("Token Bearer (não aparece na tela): ").strip()

        if not base_url.startswith(("http://", "https://")):
            raise CommandError("Base URL precisa começar com http:// ou https://")
        if not token:
            raise CommandError("Token não pode ser vazio.")

        credentials = {"base_url": base_url, "token": token}

        ds, created = OrganizationDataSource.objects.get_or_create(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
            defaults={"priority": 100, "is_active": True},
        )
        ds.set_credentials(credentials)
        ds.is_active = True
        ds.save()

        masked = f"{token[:3]}…{token[-3:]}" if len(token) > 8 else "***"
        action = "Criada" if created else "Atualizada"
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Resumo:"))
        self.stdout.write(f"  Base URL:     {base_url}")
        self.stdout.write(f"  Token (mask): {masked}")
        self.stdout.write(f"  DataSource:   {action} (OPA/ATENDIMENTO)")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "✓ Credenciais criptografadas com Fernet no DB.\n"
            "  Próximo passo: python manage.py sync_opasuite "
            f"{org_slug} --days 90\n"
        ))
