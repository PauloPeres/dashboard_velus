"""Testes do RBAC por grupo de acesso (#65).

Cobre `OrganizationMembership.allowed_page_keys` e o enforcement por
`PageAccessMiddleware`: owner vê tudo, membro com grupo só as abas do grupo,
membro sem grupo mantém acesso total (compat), grupo vazio → sem acesso.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.tenancy.models import AccessGroup, Organization, OrganizationMembership, User


def _member(
    org: Organization, *, email: str, group: AccessGroup | None,
    role: str = OrganizationMembership.Role.MEMBER,
) -> User:
    user = User.objects.create_user(email=email)
    OrganizationMembership.objects.create(
        user=user, organization=org, role=role, is_active=True, access_group=group,
    )
    return user


def _group(org: Organization, *, name: str, pages: list[str]) -> AccessGroup:
    return AccessGroup.objects.create(organization=org, name=name, allowed_pages=pages)


@pytest.mark.django_db
class TestAllowedPageKeys:
    def test_owner_sees_all(self, organization_a: Organization) -> None:
        u = _member(organization_a, email="o@a.test", group=None,
                    role=OrganizationMembership.Role.OWNER)
        m = u.get_active_membership()
        assert m.allowed_page_keys() == {"*"}

    def test_member_with_group_restricted(self, organization_a: Organization) -> None:
        g = _group(organization_a, name="Comercial", pages=["sales", "customers"])
        u = _member(organization_a, email="m@a.test", group=g)
        assert u.get_active_membership().allowed_page_keys() == {"sales", "customers"}

    def test_member_without_group_full_access(
        self, organization_a: Organization
    ) -> None:
        u = _member(organization_a, email="m2@a.test", group=None)
        assert u.get_active_membership().allowed_page_keys() == {"*"}


@pytest.mark.django_db
@pytest.mark.filterwarnings("ignore:No directory at:UserWarning")
class TestPageAccessMiddleware:
    def test_member_can_access_allowed_page(
        self, client: Any, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Exec", pages=["executive"])
        u = _member(organization_a, email="e@a.test", group=g)
        client.force_login(u)
        assert client.get("/executive/").status_code == 200

    def test_member_denied_redirects_to_allowed(
        self, client: Any, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Exec", pages=["executive"])
        u = _member(organization_a, email="e2@a.test", group=g)
        client.force_login(u)
        resp = client.get("/financial/")
        assert resp.status_code == 302
        assert resp.url == "/executive/"  # cai na 1ª aba permitida

    def test_member_empty_group_no_access(
        self, client: Any, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Nada", pages=[])
        u = _member(organization_a, email="e3@a.test", group=g)
        client.force_login(u)
        resp = client.get("/executive/")
        assert resp.status_code == 302
        assert resp.url == "/no-access/"

    def test_member_without_group_reaches_page(
        self, client: Any, organization_a: Organization
    ) -> None:
        u = _member(organization_a, email="e4@a.test", group=None)
        client.force_login(u)
        assert client.get("/financial/").status_code == 200

    def test_owner_reaches_any_page(
        self, client: Any, organization_a: Organization
    ) -> None:
        u = _member(organization_a, email="ow@a.test", group=None,
                    role=OrganizationMembership.Role.OWNER)
        client.force_login(u)
        assert client.get("/financial/").status_code == 200

    def test_nav_hides_unallowed(
        self, client: Any, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Exec", pages=["executive"])
        u = _member(organization_a, email="e5@a.test", group=g)
        client.force_login(u)
        html = client.get("/executive/").content
        assert b"Executivo" in html
        # Labels que só existem no nav (não no conteúdo da página executiva).
        assert b"Vendas / CRM" not in html
        assert b"QA de Atendimento" not in html

    def test_settings_owner_only(
        self, client: Any, organization_a: Organization
    ) -> None:
        g = _group(organization_a, name="Exec", pages=["executive"])
        u = _member(organization_a, email="e6@a.test", group=g)
        client.force_login(u)
        assert client.get("/settings/").status_code == 302  # membro não-owner barrado
