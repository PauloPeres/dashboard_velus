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

    # Marca da empresa nas telas que outras pessoas veem — hoje a TV de parede,
    # amanhã o relatório impresso. Configuração da organização, e não constante
    # no template, porque o sistema é multi-tenant: a palavra "VELUS" escrita no
    # HTML apareceria na sala de outro cliente no dia em que houver um.
    #
    # É uma URL, não um arquivo: o logo já está publicado no site da empresa, e
    # guardar upload traria armazenamento, versão e permissão para resolver um
    # problema que uma linha de configuração resolve. Quando ela está vazia, a
    # tela escreve o nome da organização — que é sempre melhor que um espaço em
    # branco onde deveria haver identidade.
    logo_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text=_("URL pública do logo, exibida no painel de parede."),
    )

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

    # Preferências opt-in de digest de risco de churn por email (default off).
    churn_digest_weekly = models.BooleanField(
        _("Digest semanal de risco de churn"), default=False
    )
    churn_digest_monthly = models.BooleanField(
        _("Digest mensal de risco de churn"), default=False
    )

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

    def get_active_membership(self) -> OrganizationMembership | None:
        """Membership ativa (com role + grupo de acesso) da org ativa."""
        return (
            self.memberships
            .filter(is_active=True, organization__is_active=True)
            .select_related("organization", "access_group")
            .first()
        )


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

    # Grupo de acesso: define quais abas o usuário enxerga. OWNER ignora (vê
    # tudo). Nulo = sem restrição (acesso total) — a restrição é opt-in ao
    # colocar o usuário num grupo. Ver allowed_page_keys().
    access_group = models.ForeignKey(
        "tenancy.AccessGroup",
        on_delete=models.SET_NULL,
        related_name="memberships",
        null=True,
        blank=True,
    )

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

    @property
    def is_owner(self) -> bool:
        return self.role == self.Role.OWNER

    def allowed_page_keys(self) -> set[str]:
        """Chaves de página que esta membership pode acessar.

        - OWNER: todas (sentinela '*').
        - COM grupo de acesso: só as páginas do grupo (restrição opt-in).
        - SEM grupo: todas ('*') — preserva o comportamento atual; a restrição
          só entra quando o usuário é colocado num grupo. Evita trancar usuários
          existentes por engano ao introduzir o RBAC.
        """
        if self.is_owner:
            return {"*"}
        if self.access_group_id and self.access_group:
            return set(self.access_group.allowed_pages or [])
        return {"*"}


# =============================================================================
# AccessGroup — grupo de permissões (quais abas) por organização
# =============================================================================
class AccessGroup(models.Model):
    """Grupo de permissões de uma organização: um nome + o conjunto de abas
    (page keys) que os membros do grupo podem ver/acessar.

    Facilita a gestão: cria-se o grupo uma vez e atribui-se usuários a ele
    (`OrganizationMembership.access_group`). OWNER ignora grupos (vê tudo).
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="access_groups",
    )
    name = models.CharField(max_length=100)
    allowed_pages = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        verbose_name = _("Grupo de acesso")
        verbose_name_plural = _("Grupos de acesso")
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"],
                name="unique_access_group_name_per_org",
            ),
        ]
        indexes = [
            models.Index(fields=["organization"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} @ {self.organization.slug} ({len(self.allowed_pages or [])} abas)"


# =============================================================================
# OrganizationInvite — registro de convite (auditoria + reenvio)
# =============================================================================
class OrganizationInvite(models.Model):
    """Registro de um convite de usuário (#66).

    Como o signup público é fechado, o convite PROVISIONA a conta na hora
    (cria User + OrganizationMembership) e dispara um e-mail de definir senha
    (reset do allauth). Este model guarda a auditoria e permite reenviar.
    """

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="invites",
    )
    email = models.EmailField()
    role = models.CharField(
        max_length=16,
        choices=OrganizationMembership.Role.choices,
        default=OrganizationMembership.Role.MEMBER,
    )
    access_group = models.ForeignKey(
        "tenancy.AccessGroup",
        on_delete=models.SET_NULL,
        related_name="invites",
        null=True,
        blank=True,
    )
    invited_by = models.ForeignKey(
        "tenancy.User",
        on_delete=models.SET_NULL,
        related_name="sent_invites",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        "tenancy.User",
        on_delete=models.CASCADE,
        related_name="invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Convite de organização")
        verbose_name_plural = _("Convites de organização")
        indexes = [
            models.Index(fields=["organization", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"convite {self.email} @ {self.organization.slug} ({self.role})"


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


class DisplayDevice(models.Model):
    """Uma TV pareada a um painel de parede (P0 do plano do painel de TV).

    Não é `TenantModel` de propósito: o dispositivo **nasce sem organização** —
    na tela de pareamento ele ainda não pertence a ninguém, e só ganha dono
    quando alguém autenticado aprova. Um TenantModel exigiria org no contexto
    justamente no momento em que ela não existe.

    Três segredos, com papéis diferentes, e a confusão entre eles é o buraco que
    o desenho evita:

    - `code` é o que **aparece na TV**. Curto, efêmero, de uso único. Quem
      fotografa a tela consegue no máximo tentar aprovar um pareamento — o que
      exige login;
    - `device_token_hash` autentica **a TV consultando o status**. Fica só no
      navegador dela, nunca na tela, e é o que garante que a credencial vá para
      quem pediu o pareamento, não para quem fotografou o QR;
    - `display_token_hash` é a **credencial final**, emitida na aprovação. Vive
      num cookie do navegador da TV e é revogável.

    Os três são guardados como hash: um dump do banco não vira TV alheia no ar.
    """

    panel_key = models.CharField(
        max_length=32,
        help_text=_("Painel que esta TV abre — uma TV do NOC não abre o executivo."),
    )
    name = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text=_("Como a sala chama esta TV ('TV da bancada')."),
    )

    code = models.CharField(
        max_length=12,
        unique=True,
        null=True,
        blank=True,
        help_text=_(
            "Código curto mostrado na TV. Efêmero e de uso único — vira NULL "
            "quando é consumido, porque código que sobrevive ao uso dá a alguém "
            "uma segunda chance de aprovar a mesma TV."
        ),
    )
    device_token_hash = models.CharField(max_length=64)
    display_token_hash = models.CharField(max_length=64, blank=True, default="")

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="display_devices",
        null=True,
        blank=True,
    )
    approved_by = models.ForeignKey(
        "tenancy.User",
        on_delete=models.SET_NULL,
        related_name="approved_displays",
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Dispositivo de painel")
        verbose_name_plural = _("Dispositivos de painel")
        indexes = [
            models.Index(fields=["organization", "revoked_at"]),
        ]

    def __str__(self) -> str:
        estado = "revogada" if self.revoked_at else ("pareada" if self.approved_at else "aguardando")
        return f"TV {self.name or self.code} ({estado})"

    @property
    def is_active(self) -> bool:
        return bool(self.approved_at) and self.revoked_at is None
