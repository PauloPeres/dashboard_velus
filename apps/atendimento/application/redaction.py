"""Redação de PII antes de enviar conversas a uma IA externa (LGPD).

A IA supervisora de QA manda o texto das conversas pra API do Claude. Conversas
de ISP carregam dado pessoal (CPF/CNPJ, telefone, e-mail, nome). Este módulo
mascara esses dados *antes* da saída do cluster, trocando por marcadores
(`[CPF]`, `[NOME]`, ...). É melhor pecar por excesso de redação: o juiz de QA
avalia tom/resolução/SLA, não precisa do dado pessoal cru.

Funções puras (texto → texto) → testáveis isoladamente.
"""

from __future__ import annotations

import re

# Ordem importa: CNPJ (14 díg.) antes de CPF (11 díg.), e ambos antes de
# telefone (8+ díg.), pra um CPF cru não ser mascarado como telefone.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
# Telefone BR: DDD opcional, 8–9 dígitos no número, +55 opcional.
_PHONE = re.compile(
    r"(?:\+55\s?)?(?:\(?\d{2}\)?[\s.-]?)?\d{4,5}[\s.-]?\d{4}\b"
)
# Token de nome a mascarar: alfabético, ≥ 4 letras (evita "de"/"da"/"Sr").
_NAME_STOPWORDS = frozenset(
    {"the", "para", "pela", "pelo", "como", "esta", "este", "isso"}
)


def _only_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def redact(
    text: str,
    *,
    names: tuple[str, ...] = (),
    documents: tuple[str, ...] = (),
) -> str:
    """Mascara PII de `text`.

    `names`/`documents` são pistas conhecidas do contexto (nome e documento do
    cliente do atendimento) — mascaradas explicitamente além dos padrões gerais.
    """
    if not text:
        return text

    redacted = _EMAIL.sub("[EMAIL]", text)
    redacted = _CNPJ.sub("[CNPJ]", redacted)
    redacted = _CPF.sub("[CPF]", redacted)
    redacted = _PHONE.sub("[TELEFONE]", redacted)

    # Documentos conhecidos (formatados ou só dígitos) que escaparam dos padrões.
    for doc in documents:
        digits = _only_digits(doc)
        if len(digits) >= 11:
            redacted = redacted.replace(doc, "[DOCUMENTO]")
            redacted = redacted.replace(digits, "[DOCUMENTO]")

    # Nome do cliente: a string completa e cada token alfabético ≥ 4 letras.
    for name in names:
        name = name.strip()
        if not name:
            continue
        redacted = re.sub(re.escape(name), "[NOME]", redacted, flags=re.IGNORECASE)
        for token in name.split():
            if len(token) >= 4 and token.isalpha() and token.lower() not in _NAME_STOPWORDS:
                redacted = re.sub(
                    rf"\b{re.escape(token)}\b", "[NOME]", redacted, flags=re.IGNORECASE
                )

    return redacted
