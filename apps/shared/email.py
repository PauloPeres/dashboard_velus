"""Utilidades de e-mail compartilhadas (#95).

Antes deste módulo, uma falha de envio virava só `email_sent=False`. Com backend
real (Mailgun via API HTTP), o provedor devolve motivo — chave inválida (401),
domínio não verificado (403), destinatário recusado (400) — e esse motivo
precisa chegar no log.

Regra de ouro: **a chave de API nunca pode vazar pro log**. `extract_error` roda
o texto do erro por uma redação antes de devolver.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings

_MAX_LEN = 800
# Formatos de chave do Mailgun: "key-<hex>" (legado) e o token novo, longo.
_KEY_PATTERN = re.compile(r"\bkey-[0-9a-zA-Z]{8,}\b")


def redact_secrets(text: str) -> str:
    """Remove qualquer ocorrência da chave de API do texto."""
    api_key = (settings.ANYMAIL or {}).get("MAILGUN_API_KEY") or ""
    if api_key:
        text = text.replace(api_key, "***")
    return _KEY_PATTERN.sub("key-***", text)


def email_error_details(exc: BaseException) -> dict[str, Any]:
    """Campos estruturados descrevendo a falha de envio, prontos pra log.

    Devolve sempre `error` (mensagem redigida e truncada) e, quando o provedor
    respondeu, `status_code` e `provider_response`.
    """
    details: dict[str, Any] = {"error": redact_secrets(str(exc))[:_MAX_LEN]}

    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        details["status_code"] = status_code

    response = getattr(exc, "response", None)
    body = getattr(response, "text", None) or getattr(response, "content", None)
    if body:
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        details["provider_response"] = redact_secrets(str(body))[:_MAX_LEN]

    return details


def describe_backend() -> str:
    """Descrição legível do transporte ativo — usada no command de verificação."""
    backend = settings.EMAIL_BACKEND
    if getattr(settings, "EMAIL_ENABLED", False):
        anymail = settings.ANYMAIL or {}
        return (
            f"{backend} (Mailgun · domínio={anymail.get('MAILGUN_SENDER_DOMAIN')} · "
            f"api={anymail.get('MAILGUN_API_URL')})"
        )
    return f"{backend} (envio real DESLIGADO — nada sai do processo)"
