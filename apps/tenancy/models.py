"""Models de tenancy — Organization (tenant raiz), User, Membership, DataSource.

ATENÇÃO: nenhum model deste arquivo herda de `apps.shared.TenantModel`.
Tenancy É a base de multi-tenancy; não pode depender de si mesma.
- Organization é o tenant raiz (sem `organization` FK — ela É a organização).
- User existe sem org direta (vínculo via OrganizationMembership).
- OrganizationDataSource tem `organization` FK explícito (não via TenantModel).
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _
from simple_history.models import HistoricalRecords

from apps.integrations.shared.enums import Capability, SourceType
from apps.shared.fields import EncryptedTextField

from .managers import UserManager


# =============================================================================
# Organization — tenant raiz
# =============================================================================
class Organization(models.Model):
    """Raiz de todo isolamento de dados.

    Tudo (Customer, Contract, Invoice, Scenario...) é filho dela via FK.
    Quando uma org é desativada (`is_active=False`), seus syncs param e
    usuários perdem acesso, mas os dados ficam preservados para auditoria.
    """

    slug = models.SlugField(
        unique=True,
        max_length=64,
        help_text=_("Identificador usado em URLs, CLI e filas Celery."),
    )
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Organização")
        verbose_name_plural = _("Organizações")
        indexes = [
            models.Index(fields=["is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug})"

    @property
    def celery_queue_name(self) -> str:
        """Nome da fila Celery dedicada a este tenant."""
        return f"tenant_{self.slug}"


# =============================================================================
# User — autenticação por email
# =============================================================================
class User(AbstractUser):
    """User com email como identificador (sem username).

    Não tem FK direto pra Organization — vínculo via OrganizationMembership.
    Pra obter a org ativa, use `user.get_active_organization()`.
    """

    username = None  # type: ignore[assignment]  # remove o campo do AbstractUser
    email = models.EmailField(_("email"), unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()  # type: ignore[assignment]

    history = HistoricalRecords()

    class Meta(AbstractUser.Meta):
        verbose_name = _("Usuário")
        verbose_name_plural = _("Usuários")

    def __str__(self) -> str:
        return self.email

    def get_active_organization(self) -> Organization | None:
        """Retorna a primeira Organization ativa via membership ativa.

        No MVP cada usuário tem uma única membership ativa. Quando suportarmos
        multi-org, este método vira "org selecionada na sessão" (default = 1ª).
        """
        membership = (
            self.memberships
            .filter(is_active=True, organization__is_active=True)
            .select_related("organization")
            .first()
        )
        return membership.organization if membership else None


# =============================================================================
# OrganizationMembership — User ↔ Organization (com role)
# =============================================================================
class OrganizationMembership(models.Model):
    """Vínculo entre User e Organization, com role.

    Permite múltiplas memberships por User no futuro sem migração de schema.
    `is_active=False` revoga acesso sem deletar (preserva audit log).
    """

    class Role(models.TextChoices):
        OWNER = "OWNER", _("Owner")        # admin total, billing, criar/remover users
        MEMBER = "MEMBER", _("Member")     # CRUD de cenários, ler dashboards
        VIEWER = "VIEWER", _("Viewer")     # só leitura

    user = models.ForeignKey(
        "tenancy.User",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    is_active = models.BooleanField(default=True)

    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Membership de organização")
        verbose_name_plural = _("Memberships de organização")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="unique_user_organization",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.user.email} @ {self.organization.slug} ({self.role})"


# =============================================================================
# OrganizationDataSource — configuração de adapter externo por org
# =============================================================================
class OrganizationDataSource(models.Model):
    """Configuração que liga uma Organization a um adapter (IXC, ContaAzul, ...)
    para uma capability específica (Customers, Invoices, ...).

    Permite:
    - Múltiplas fontes simultâneas pra uma org (IXC + ContaAzul ambos pra Customers).
    - Ordem por `priority` (maior = mais prioritário em merge).
    - Credenciais criptografadas (Fernet) no DB.
    - Desativação sem deletar (auditoria preservada).

    Resolução em runtime: `SourceRegistry.get_sources(org, capability)` retorna
    lista ordenada por priority. Ver AGENT.md §1.6.
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="data_sources",
    )
    source_type = models.CharField(max_length=32, choices=SourceType.choices)
    capability = models.CharField(max_length=32, choices=Capability.choices)

    # Credenciais como JSON criptografado (formato específico por source_type).
    # Ex.: IXC → {"base_url": "https://erp.cliente.com.br", "user_id": "1", "api_token": "..."}
    credentials_encrypted = EncryptedTextField(
        help_text=_("JSON serializado com credenciais. Formato depende do source_type."),
    )

    priority = models.PositiveIntegerField(
        default=100,
        help_text=_("Maior = mais prioritário em merge entre fontes."),
    )
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Fonte de dados")
        verbose_name_plural = _("Fontes de dados")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "source_type", "capability"],
                name="unique_org_source_capability",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_active"]),
            models.Index(fields=["source_type", "capability"]),
        ]

    def __str__(self) -> str:
        return f"{self.organization.slug} → {self.source_type}:{self.capability}"

    # -------------------------------------------------------------------------
    # Helpers de credenciais
    # -------------------------------------------------------------------------
    def get_credentials(self) -> dict[str, Any]:
        """Descriptografa, parseia o JSON e devolve dict."""
        raw = self.credentials_encrypted  # já vem descriptografado pelo EncryptedTextField
        if raw is None or raw == "":
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def set_credentials(self, credentials: dict[str, Any]) -> None:
        """Serializa pra JSON e armazena (criptografia é automática pelo field)."""
        self.credentials_encrypted = json.dumps(credentials, ensure_ascii=False)
