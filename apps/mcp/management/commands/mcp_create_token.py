"""Emite um token de acesso ao servidor MCP para uma organização.

Uso:
    python manage.py mcp_create_token velus --name "Claude Desktop do Paulo"

O segredo é exibido UMA única vez — copie na hora. Só o hash fica no banco.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.mcp.models import McpToken
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization


class Command(BaseCommand):
    help = "Emite um token Bearer para o servidor MCP de uma organização"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("org_slug", type=str, help="Slug da organização")
        parser.add_argument(
            "--name",
            type=str,
            default="MCP token",
            help="Identificação humana do token",
        )

    @allow_cross_tenant(reason="mcp_create_token: emitir credencial fora de request")
    def handle(self, *args, **opts) -> None:
        org_slug: str = opts["org_slug"]
        name: str = opts["name"]

        try:
            org = Organization.objects.get(slug=org_slug)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organização '{org_slug}' não existe.") from exc

        _, raw = McpToken.issue(organization=org, name=name)

        self.stdout.write(self.style.SUCCESS(f"\n✓ Token criado para '{org_slug}'"))
        self.stdout.write(
            "  Guarde o segredo abaixo AGORA — ele não será exibido novamente:\n"
        )
        self.stdout.write(f"    {raw}\n")
