"""Testes da tela self-service de grupos de acesso (owner-only, #70)."""

from __future__ import annotations

from typing import Any

import pytest

from apps.tenancy.models import (
    AccessGroup,
    Organization,
    OrganizationMembership,
    User,
)

URL = "/settings/acesso/"


def _member(org: Organization, *, email: str, role: str = "MEMBER") -> User:
    u = User.objects.create_user(email=email)
    OrganizationMembership.objects.create(
        user=u, organization=org, role=role, is_active=True,
    )
    return u


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestAccessManagement:
    def test_non_owner_blocked(self, client: Any, organization_a: Organization) -> None:
        u = _member(organization_a, email="m@a.test")
        client.force_login(u)
        assert client.get(URL).status_code == 302  # middleware owner-only

    def test_owner_renders(self, client: Any, user_a: User) -> None:
        client.force_login(user_a)
        html = client.get(URL).content
        assert b"Novo grupo" in html

    def test_owner_creates_group(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        client.force_login(user_a)
        resp = client.post(URL, {
            "action": "create_group", "name": "Comercial",
            "pages": ["sales", "customers", "inexistente"],
        })
        assert resp.status_code == 302
        g = AccessGroup.objects.get(organization=organization_a, name="Comercial")
        assert set(g.allowed_pages) == {"sales", "customers"}  # chave inválida filtrada

    def test_owner_updates_and_deletes_group(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        g = AccessGroup.objects.create(
            organization=organization_a, name="X", allowed_pages=["executive"]
        )
        client.force_login(user_a)
        client.post(URL, {
            "action": "update_group", "group_id": str(g.id),
            "name": "X2", "pages": ["financial"],
        })
        g.refresh_from_db()
        assert g.name == "X2"
        assert g.allowed_pages == ["financial"]

        client.post(URL, {"action": "delete_group", "group_id": str(g.id)})
        assert not AccessGroup.objects.filter(id=g.id).exists()

    def test_owner_assigns_member_to_group(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        g = AccessGroup.objects.create(
            organization=organization_a, name="G", allowed_pages=["executive"]
        )
        member = _member(organization_a, email="p@a.test")
        m = member.get_active_membership()
        client.force_login(user_a)
        client.post(URL, {
            "action": "assign", "membership_id": str(m.id), "group_id": str(g.id),
        })
        m.refresh_from_db()
        assert m.access_group == g
        # tirar do grupo (sem grupo = vê tudo)
        client.post(URL, {
            "action": "assign", "membership_id": str(m.id), "group_id": "",
        })
        m.refresh_from_db()
        assert m.access_group is None
