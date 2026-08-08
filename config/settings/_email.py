"""Resolução dos settings de e-mail (#95).

Isolado do `base.py` pra ser testável direto: a mesma função decide o backend
com e sem as variáveis do Mailgun, sem precisar reimportar módulo de settings.

Regra: **só liga o transporte real quando há chave E domínio remetente**. Sem
isso, mantém o backend de fallback (console em dev, locmem em teste) — quem não
tem a chave (dev local, CI) não vê nenhuma mudança de comportamento.

NUNCA importe em código de app — leia via `django.conf.settings`.
"""

from __future__ import annotations

from typing import Any

MAILGUN_BACKEND = "anymail.backends.mailgun.EmailBackend"


def _from_address_domain(address: str) -> str:
    """Extrai o domínio de um `Nome <user@dominio>` ou `user@dominio`."""
    addr = address.strip().rstrip(">")
    _, _, domain = addr.rpartition("@")
    return domain.strip().lower()


def build_email_settings(
    *,
    api_key: str,
    sender_domain: str,
    api_url: str,
    fallback_backend: str,
    default_from_email: str,
) -> dict[str, Any]:
    """Devolve o bloco de settings de e-mail já resolvido.

    Retorna sempre as mesmas chaves: EMAIL_ENABLED, EMAIL_BACKEND, ANYMAIL,
    DEFAULT_FROM_EMAIL, SERVER_EMAIL.
    """
    api_key = (api_key or "").strip()
    sender_domain = (sender_domain or "").strip()
    enabled = bool(api_key and sender_domain)

    if not enabled:
        return {
            "EMAIL_ENABLED": False,
            "EMAIL_BACKEND": fallback_backend,
            "ANYMAIL": {},
            "DEFAULT_FROM_EMAIL": default_from_email,
            "SERVER_EMAIL": default_from_email,
        }

    # O Mailgun recusa remetente fora do domínio verificado (403). O default de
    # dev (`@velus.local`) e o subdomínio usado no ConfigMap não valem — força
    # `noreply@<sender_domain>` em vez de deixar o envio quebrar em runtime.
    from_email = default_from_email
    if _from_address_domain(from_email) != sender_domain.lower():
        from_email = f"Velus Dashboard <noreply@{sender_domain}>"

    return {
        "EMAIL_ENABLED": True,
        "EMAIL_BACKEND": MAILGUN_BACKEND,
        "ANYMAIL": {
            "MAILGUN_API_KEY": api_key,
            "MAILGUN_SENDER_DOMAIN": sender_domain,
            "MAILGUN_API_URL": api_url or "https://api.mailgun.net/v3",
        },
        "DEFAULT_FROM_EMAIL": from_email,
        "SERVER_EMAIL": from_email,
    }
