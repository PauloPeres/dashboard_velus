"""Testes do sistema de convite (#66).

Signup fechado → convidar provisiona User + OrganizationMembership e dispara
e-mail de definir senha. Cobre o serviço `invite_user` e o fluxo na tela de
settings (owner-only).
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core import mail

from apps.tenancy.invites import invite_user
from apps.tenancy.models import (
    AccessGroup,
    Organization,
    OrganizationInvite,
    OrganizationMembership,
    User,
)


def _group(org: Organization, *, name: str, pages: list[str]) -> AccessGroup:
    return AccessGroup.objects.create(organization=org, name=name, allowed_pages=pages)


@pytest.mark.django_db
class TestInviteService:
    def test_provisions_user_and_membership(
        self, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Comercial", pages=["sales"])
        res = invite_user(
            organization=organization_a, email="Novo@Empresa.com",
            role="MEMBER", access_group=g, invited_by=None,
        )
        assert res.user_created is True
        u = User.objects.get(email="novo@empresa.com")  # normalizado lowercase
        assert not u.has_usable_password()  # senha inutilizável até definir
        m = res.membership
        assert m.user == u
        assert m.organization == organization_a
        assert m.role == "MEMBER"
        assert m.access_group == g
        assert m.is_active is True
        assert OrganizationInvite.objects.filter(email="novo@empresa.com").count() == 1

    def test_reinvite_updates_membership_idempotent(
        self, organization_a: Organization
    ) -> None:
        g1 = _group(organization_a, name="A", pages=["sales"])
        g2 = _group(organization_a, name="B", pages=["executive"])
        invite_user(organization=organization_a, email="p@e.com", role="MEMBER",
                    access_group=g1, invited_by=None)
        res2 = invite_user(organization=organization_a, email="p@e.com", role="VIEWER",
                           access_group=g2, invited_by=None)
        assert res2.user_created is False
        assert OrganizationMembership.objects.filter(
            user__email="p@e.com", organization=organization_a
        ).count() == 1  # não duplica
        m = res2.membership
        assert m.role == "VIEWER"
        assert m.access_group == g2


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestInviteView:
    def test_owner_sees_invite_form(
        self, client: Any, user_a: User
    ) -> None:
        client.force_login(user_a)
        html = client.get("/settings/").content
        assert b"Convidar usu" in html

    def test_owner_can_invite_and_email_sent(
        self, client: Any, user_a: User, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Comercial", pages=["sales"])
        client.force_login(user_a)
        mail.outbox.clear()
        resp = client.post("/settings/", {
            "action": "invite", "email": "convidado@empresa.com",
            "role": "MEMBER", "access_group": str(g.id),
        })
        assert resp.status_code == 200
        assert OrganizationMembership.objects.filter(
            user__email="convidado@empresa.com", organization=organization_a,
            access_group=g,
        ).exists()
        # e-mail de definir senha disparado (backend locmem em teste)
        assert len(mail.outbox) == 1
        assert "convidado@empresa.com" in mail.outbox[0].to
