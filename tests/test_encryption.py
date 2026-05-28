"""Testes da EncryptedTextField — Fernet roundtrip + persistência."""

from __future__ import annotations

import pytest

from apps.integrations.shared.enums import Capability, SourceType
from apps.tenancy.models import Organization, OrganizationDataSource


@pytest.mark.django_db
class TestEncryptedTextField:
    """Credenciais entram criptografadas, saem em claro — transparente pra app."""

    def test_roundtrip_set_get_credentials(
        self, organization_a: Organization
    ) -> None:
        ds = OrganizationDataSource(
            organization=organization_a,
            source_type=SourceType.IXC.value,
            capability=Capability.CUSTOMERS.value,
            priority=100,
            is_active=True,
        )
        creds = {
            "base_url": "https://erp.example.com.br",
            "user_id": "1",
            "api_token": "super-secret-token-9aa1dfca",
        }
        ds.set_credentials(creds)
        ds.save()

        # Read back via outra query — ciclo completo no DB
        ds2 = OrganizationDataSource.objects.get(pk=ds.pk)
        assert ds2.get_credentials() == creds

    def test_raw_bytes_in_db_are_encrypted(
        self, organization_a: Organization
    ) -> None:
        """Confirma que ciphertext no DB começa com prefixo Fernet."""
        from django.db import connection

        ds = OrganizationDataSource(
            organization=organization_a,
            source_type=SourceType.IXC.value,
            capability=Capability.CUSTOMERS.value,
            priority=100,
            is_active=True,
        )
        ds.set_credentials({"api_token": "claro-no-codigo"})
        ds.save()

        with connection.cursor() as c:
            c.execute(
                "SELECT credentials_encrypted FROM tenancy_organizationdatasource WHERE id = %s",
                [ds.pk],
            )
            raw = bytes(c.fetchone()[0])

        # Fernet tokens são Base64 url-safe; bytes começam com b"gAAAAA"
        # (URL-safe Base64 do primeiro byte do header Fernet = 0x80)
        assert raw.startswith(b"gAAAAA"), (
            f"DB content not encrypted with Fernet — leak? raw[:20]={raw[:20]!r}"
        )
        assert b"claro-no-codigo" not in raw, "Plaintext leaked into DB!"
