"""Variáveis de ambiente tipadas via pydantic-settings.

Source-of-truth única — carrega .env em dev, K8s Secrets em prod.
Falha cedo (no import do settings) se var obrigatória faltar.

NUNCA importe diretamente em código de app — leia via `django.conf.settings`.
Este módulo é interno aos settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """Schema das env vars do projeto.

    Convenção: prefixo `DJANGO_` apenas onde a var é claramente "do Django"
    (nome batendo com setting nativo). Outras vars sem prefixo.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Ambiente
    # -------------------------------------------------------------------------
    DJANGO_ENV: Literal["development", "production", "test"] = "development"

    # -------------------------------------------------------------------------
    # Django core
    # -------------------------------------------------------------------------
    DJANGO_SECRET_KEY: SecretStr
    DJANGO_DEBUG: bool = False
    DJANGO_ALLOWED_HOSTS: str = "localhost,127.0.0.1"  # CSV
    DJANGO_TIME_ZONE: str = "America/Sao_Paulo"
    DJANGO_LANGUAGE_CODE: str = "pt-br"

    # -------------------------------------------------------------------------
    # Database — DATABASE_URL no formato postgres://user:pass@host:port/db
    # -------------------------------------------------------------------------
    DATABASE_URL: str = Field(..., min_length=1)

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_postgres_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("postgres", "postgresql"):
            raise ValueError(f"DATABASE_URL deve ser postgres://...; recebido scheme={parsed.scheme!r}")
        if not parsed.hostname or not parsed.path:
            raise ValueError("DATABASE_URL precisa de host e nome do banco")
        return v

    # -------------------------------------------------------------------------
    # Redis / Celery
    # -------------------------------------------------------------------------
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # -------------------------------------------------------------------------
    # Criptografia (Fernet) — credenciais externas no DB
    # -------------------------------------------------------------------------
    FERNET_KEY: SecretStr

    @field_validator("FERNET_KEY")
    @classmethod
    def _validate_fernet_key(cls, v: SecretStr) -> SecretStr:
        # Aceita placeholder em dev; em prod, valida formato real.
        raw = v.get_secret_value()
        if raw == "change-me-generate-with-fernet":
            return v  # placeholder dev — settings/development relaxa
        try:
            from cryptography.fernet import Fernet

            Fernet(raw.encode())
        except Exception as exc:
            raise ValueError(
                "FERNET_KEY inválida. Gere com: "
                "python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
            ) from exc
        return v

    # -------------------------------------------------------------------------
    # Google OAuth (django-allauth)
    # -------------------------------------------------------------------------
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: SecretStr = SecretStr("")

    # -------------------------------------------------------------------------
    # Email
    # -------------------------------------------------------------------------
    EMAIL_BACKEND: str = "django.core.mail.backends.console.EmailBackend"
    DEFAULT_FROM_EMAIL: str = "Velus Dashboard <noreply@velus.local>"

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["console", "json"] = "console"

    # -------------------------------------------------------------------------
    # Observabilidade (opcional)
    # -------------------------------------------------------------------------
    SENTRY_DSN: str = ""

    # -------------------------------------------------------------------------
    # Opa! Suite (atendimento/WhatsApp) — base read-only p/ ingestão
    # -------------------------------------------------------------------------
    OPA_LINK: str = ""  # ex.: https://opasuite.sorocabana.net.br/
    OPA_TOKEN: SecretStr = SecretStr("")  # JWT Bearer

    # -------------------------------------------------------------------------
    # IA supervisora de atendimento (LLM-as-judge) — QA das conversas (#50/#51)
    # -------------------------------------------------------------------------
    # Chave global da API Anthropic; vazia desliga o QA por LLM (fallback: só o
    # léxico/heurística). Em prod vira K8s Secret. Haiku é barato e suficiente
    # pra avaliar conversa contra rubrica; pode subir pra Sonnet se precisar.
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    QA_LLM_MODEL: str = "claude-haiku-4-5-20251001"
    QA_LLM_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Servidor MCP (read-only, autenticado por token por organização)
    # -------------------------------------------------------------------------
    MCP_ENABLED: bool = False
    MCP_HOST: str = "0.0.0.0"  # noqa: S104 — bind interno ao Pod, exposto via Ingress
    MCP_PORT: int = 8800
    # Hosts aceitos no header Host (proteção anti DNS-rebinding do FastMCP). CSV.
    # Vazio = proteção desligada (conveniência em dev/local). Em prod, setar o
    # host público do Ingress (ex.: "mcp.seujaime.com").
    MCP_ALLOWED_HOSTS: str = ""


# Singleton — importado por base.py
env = Settings()
