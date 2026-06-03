"""Tokens de acesso ao servidor MCP.

McpToken NÃO é um TenantModel: a autenticação precede a resolução da org
(o token é justamente o que identifica a org). Por isso é um model comum com
FK para Organization, consultado via @allow_cross_tenant durante o handshake.

O segredo do token nunca é persistido em claro — guardamos apenas o hash
SHA-256. O prefixo (primeiros chars) fica em claro só para exibição/auditoria
("qual token foi esse?"), nunca é suficiente para autenticar.
"""

from __future__ import annotations

import hashlib
import secrets

from django.db import models
from django.utils import timezone

from apps.shared.decorators import allow_cross_tenant
from apps.shared.mixins import TimestampedMixin

_TOKEN_PREFIX = "velus_mcp_"  # noqa: S105 — prefixo público, não é segredo
_PREFIX_DISPLAY_LEN = len(_TOKEN_PREFIX) + 8


def _hash_token(raw: str) -> str:
    """SHA-256 hex do token cru — comparação em tempo constante via secrets."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class McpToken(TimestampedMixin):
    """Credencial Bearer de uma organização para o servidor MCP read-only."""

    organization = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="mcp_tokens",
        verbose_name="Organização",
    )
    name = models.CharField(
        max_length=120,
        help_text="Identificação humana do token (ex.: 'Claude Desktop do Paulo').",
    )
    prefix = models.CharField(
        max_length=32,
        db_index=True,
        help_text="Início do token em claro — só para exibição/auditoria.",
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 do token. O segredo cru nunca é persistido.",
    )
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Token MCP"
        verbose_name_plural = "Tokens MCP"
        indexes = [
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @classmethod
    def issue(cls, *, organization, name: str) -> tuple[McpToken, str]:
        """Cria um token novo e devolve (instância, segredo_cru).

        O segredo cru só existe neste retorno — não há como recuperá-lo depois.
        """
        raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            organization=organization,
            name=name,
            prefix=raw[:_PREFIX_DISPLAY_LEN],
            token_hash=_hash_token(raw),
        )
        return token, raw


@allow_cross_tenant(reason="autenticação MCP: resolver org a partir do Bearer token")
def authenticate_token(raw: str | None):
    """Resolve o Bearer token cru para a Organization dona, ou None.

    Cross-tenant é necessário porque a org ainda não está no contexto — o token
    é exatamente o que vai defini-la. Comparação por hash (índice unique), sem
    varredura. Atualiza last_used_at em acesso válido.
    """
    if not raw:
        return None
    token = (
        McpToken.objects.filter(token_hash=_hash_token(raw), is_active=True)
        .select_related("organization")
        .first()
    )
    if token is None:
        return None
    token.last_used_at = timezone.now()
    token.save(update_fields=["last_used_at", "updated_at"])
    return token.organization
