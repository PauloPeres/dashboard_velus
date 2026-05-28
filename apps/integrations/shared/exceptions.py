"""Exceções da camada de integração externa.

Hierarquia projetada para retry policies claras:
    AdapterError
    ├── AdapterAuthError       — 401/403 — NÃO retryable, alerta humano
    ├── AdapterTransientError  — 5xx, timeout, rate limit — retry exponencial
    ├── AdapterClientError     — 4xx (não-auth) — NÃO retryable, bug de adapter ou API
    └── AdapterContractError   — Pydantic falhou validando resposta — bug
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base de toda falha em adapter externo."""


class AdapterAuthError(AdapterError):
    """Falha de autenticação (HTTP 401/403). Não retryable."""


class AdapterTransientError(AdapterError):
    """Falha temporária (5xx, timeout, rate-limit). Retryable com backoff."""


class AdapterClientError(AdapterError):
    """4xx não-auth (400, 404, 422, etc.). Não retryable — indica bug."""


class AdapterContractError(AdapterError):
    """Resposta da API quebrou o contrato (Pydantic validação falhou).

    Quase sempre = sistema externo mudou schema entre updates.
    Falha alto pra forçar revisão do adapter, não corromper fact tables.
    """
