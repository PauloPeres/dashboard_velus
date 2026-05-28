"""Management command `create_organization`.

Cria/atualiza idempotentemente:
- Organization (slug único)
- User owner (por email)
- OrganizationMembership(role=OWNER, is_active=True)
- (opcional) OrganizationDataSource pra IXC com credenciais cifradas

Uso:
    python manage.py create_organization velus \\
        --name="Velus" \\
        --owner-email=p.peresjr@gmail.com

    python manage.py create_organization velus \\
        --name="Velus" \\
        --owner-email=p.peresjr@gmail.com \\
        --ixc-base-url=https://erp.example.com.br \\
        --ixc-user-id=1 \\
        --ixc-token=abc123...

Idempotência: rodar 2x não duplica nada; atualiza name/credenciais se mudaram.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from apps.integrations.shared.enums import Capability, SourceType
from apps.tenancy.models import (
    Organization,
    OrganizationDataSource,
    OrganizationMembership,
    User,
)


class Command(BaseCommand):
    help = "Cria/atualiza idempotentemente uma Organization com owner e (opcional) IXC."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "slug",
            type=str,
            help="Slug único da Organization (usado em URLs e fila Celery).",
        )
        parser.add_argument(
            "--name",
            type=str,
            required=True,
            help="Nome legível da Organization.",
        )
        parser.add_argument(
            "--owner-email",
            type=str,
            required=True,
            help="Email do User OWNER (será criado se não existir).",
        )
        parser.add_argument(
            "--owner-first-name",
            type=str,
            default="",
            help="Primeiro nome do owner (opcional).",
        )
        parser.add_argument(
            "--owner-last-name",
            type=str,
            default="",
            help="Sobrenome do owner (opcional).",
        )
        parser.add_argument(
            "--ixc-base-url",
            type=str,
            default="",
            help="URL base da API IXC (ex: https://erp.example.com.br).",
        )
        parser.add_argument(
            "--ixc-user-id",
            type=str,
            default="",
            help="user_id da API IXC (parte do Basic auth).",
        )
        parser.add_argument(
            "--ixc-token",
            type=str,
            default="",
            help="API token IXC (parte do Basic auth).",
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ARG002

        slug: str = options["slug"]
        name: str = options["name"]
        owner_email: str = options["owner_email"]
        first_name: str = options["owner_first_name"]
        last_name: str = options["owner_last_name"]
        ixc_base_url: str = options["ixc_base_url"]
        ixc_user_id: str = options["ixc_user_id"]
        ixc_token: str = options["ixc_token"]

        self._validate_ixc_args(ixc_base_url, ixc_user_id, ixc_token)

        # ---------------------------------------------------------------------
        # Organization
        # ---------------------------------------------------------------------
        org, created = Organization.objects.update_or_create(
            slug=slug,
            defaults={"name": name, "is_active": True},
        )
        action = "criada" if created else "atualizada"
        self.stdout.write(self.style.SUCCESS(f"Organization {action}: {org}"))

        # ---------------------------------------------------------------------
        # Owner User
        # ---------------------------------------------------------------------
        user, user_created = User.objects.get_or_create(
            email=owner_email,
            defaults={
                "first_name": first_name,
                "last_name": last_name,
                "is_active": True,
                "is_staff": True,  # owner é staff (acessa admin)
            },
        )
        if user_created:
            # Sem senha — autenticação via Google OAuth. Aceita reset via Django admin se necessário.
            user.set_unusable_password()
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(f"User criado: {user}"))
        else:
            self.stdout.write(f"User já existia: {user}")

        # ---------------------------------------------------------------------
        # Membership OWNER
        # ---------------------------------------------------------------------
        membership, m_created = OrganizationMembership.objects.update_or_create(
            user=user,
            organization=org,
            defaults={
                "role": OrganizationMembership.Role.OWNER,
                "is_active": True,
            },
        )
        m_action = "criada" if m_created else "garantida"
        self.stdout.write(
            self.style.SUCCESS(f"Membership {m_action}: {membership}")
        )

        # ---------------------------------------------------------------------
        # OrganizationDataSource (IXC) — opcional
        # ---------------------------------------------------------------------
        if ixc_base_url:
            credentials = {
                "base_url": ixc_base_url.rstrip("/"),
                "user_id": ixc_user_id,
                "api_token": ixc_token,
            }
            ds, ds_created = OrganizationDataSource.objects.get_or_create(
                organization=org,
                source_type=SourceType.IXC,
                capability=Capability.CUSTOMERS,
                defaults={"priority": 100, "is_active": True},
            )
            ds.set_credentials(credentials)
            ds.is_active = True
            ds.save()
            ds_action = "criada" if ds_created else "atualizada"
            self.stdout.write(
                self.style.SUCCESS(
                    f"DataSource IXC/{Capability.CUSTOMERS} {ds_action} "
                    f"(credenciais criptografadas no DB)"
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"✓ Setup completo de '{org.slug}'"))
        self.stdout.write(f"  Fila Celery: {org.celery_queue_name}")
        self.stdout.write(f"  Owner:       {user.email}")
        if ixc_base_url:
            self.stdout.write(f"  IXC:         {ixc_base_url}")

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _validate_ixc_args(base_url: str, user_id: str, token: str) -> None:
        any_set = any([base_url, user_id, token])
        all_set = all([base_url, user_id, token])
        if any_set and not all_set:
            raise CommandError(
                "Para configurar IXC, --ixc-base-url, --ixc-user-id e --ixc-token "
                "precisam ser informados juntos."
            )
