"""Serviço de convite de usuários (#66).

Signup público é fechado (NoSignupAccountAdapter), então convidar =
PROVISIONAR: cria o User (senha inutilizável) + a OrganizationMembership
(role + grupo de acesso) e dispara um e-mail de "definir senha" (reset do
allauth). A pessoa define a senha e entra — ou entra via Google, que
auto-conecta contas cujo e-mail já existe (AutoConnectSocialAdapter).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from django.db import transaction
from django.http import HttpRequest

from .models import (
    AccessGroup,
    Organization,
    OrganizationInvite,
    OrganizationMembership,
    User,
)

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class InviteResult:
    membership: OrganizationMembership
    user_created: bool
    email_sent: bool


def _send_set_password_email(email: str, request: HttpRequest | None) -> bool:
    """Dispara o e-mail de definir/redefinir senha via allauth. Retorna se enviou."""
    try:
        from allauth.account.forms import ResetPasswordForm

        form = ResetPasswordForm(data={"email": email})
        if form.is_valid():
            form.save(request)
            return True
        _logger.warning("invite_reset_email_invalid", email=email, errors=form.errors)
    except Exception:
        _logger.exception("invite_reset_email_failed", email=email)
    return False


@transaction.atomic
def invite_user(
    *,
    organization: Organization,
    email: str,
    role: str,
    access_group: AccessGroup | None,
    invited_by: User | None,
    request: HttpRequest | None = None,
) -> InviteResult:
    """Provisiona (ou atualiza) o acesso de um usuário à organização e envia o
    e-mail de definir senha. Idempotente por (user, org): reconvidar atualiza
    role/grupo e reenvia o e-mail.
    """
    email = email.strip().lower()
    if role not in OrganizationMembership.Role.values:
        role = OrganizationMembership.Role.MEMBER

    user, user_created = User.objects.get_or_create(email=email)
    if user_created:
        user.set_unusable_password()
        user.save(update_fields=["password"])

    membership, _ = OrganizationMembership.objects.update_or_create(
        user=user,
        organization=organization,
        defaults={
            "role": role,
            "access_group": access_group,
            "is_active": True,
        },
    )

    OrganizationInvite.objects.create(
        organization=organization,
        email=email,
        role=role,
        access_group=access_group,
        invited_by=invited_by,
        user=user,
    )

    email_sent = _send_set_password_email(email, request)
    _logger.info(
        "user_invited",
        org=organization.slug,
        email=email,
        role=role,
        group=access_group.name if access_group else None,
        user_created=user_created,
        email_sent=email_sent,
    )
    return InviteResult(
        membership=membership, user_created=user_created, email_sent=email_sent
    )
