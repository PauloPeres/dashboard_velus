"""Domain services do bounded context Customers — lógica de negócio pura.

Sem dependências de Django ORM, sem I/O. Testável com pytest puro.
"""

from __future__ import annotations

import re

# Apenas dígitos: 11 (CPF) ou 14 (CNPJ).
_CPF_LEN = 11
_CNPJ_LEN = 14
_DIGITS_RE = re.compile(r"\D")


def normalize_document(raw: str | None) -> str:
    """Remove tudo que não é dígito. Não valida — só normaliza.

    Adapter passa o documento já normalizado pra `CustomerDTO.document`,
    mas é seguro re-normalizar em código de domain pra defesa em profundidade.
    """
    if not raw:
        return ""
    return _DIGITS_RE.sub("", raw)


def classify_document(digits: str) -> str:
    """Classifica documento normalizado em CPF / CNPJ / INVALID."""
    if len(digits) == _CPF_LEN:
        return "CPF"
    if len(digits) == _CNPJ_LEN:
        return "CNPJ"
    return "INVALID"


def resolve_identity_key(document: str) -> str:
    """Devolve a chave de identidade lógica (cross-source).

    Mesmo cliente em IXC e em ContaAzul terá o mesmo document → mesma
    identity key. Usado pra deduplicar/merge em queries de analytics.

    Atualmente = document normalizado. Pode evoluir pra hash determinístico
    (HMAC com chave estável) se precisar de privacidade — fica isolado aqui.
    """
    return normalize_document(document)
