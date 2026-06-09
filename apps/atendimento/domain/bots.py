"""Identidade dos bots de autoatendimento (Opa! Suite).

Gi e Felipe são os bots de autoatendimento do ISP no WhatsApp — NÃO pessoas
(cuidado: "Felipe P." é atendente humano real, com sobrenome). A identificação
é por NOME normalizado porque os external_ids podem variar entre re-syncs e o
roster muda.

Fonte ÚNICA: consumida pelo juiz de QA (que usa rubrica própria pra bot) e pelo
scorecard (coorte bot vs humano). Se o roster mudar, mexe só aqui.
"""

from __future__ import annotations

import unicodedata

# Roster de bots. Paulo confirmou só Gi/Felipe (2026-06-09); a confirmar se
# surgirem outros.
BOT_ATENDENTE_NOMES = frozenset({"gi", "felipe"})


def _normalize_nome(nome: str | None) -> str:
    text = unicodedata.normalize("NFKD", nome or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.strip().lower()


def is_bot_atendente(nome: str | None) -> bool:
    """True se o atendente é um bot de autoatendimento (Gi/Felipe), não pessoa."""
    return _normalize_nome(nome) in BOT_ATENDENTE_NOMES
