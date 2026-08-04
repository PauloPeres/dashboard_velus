"""Guarda contra comentário `{# ... #}` multi-linha nos templates.

O comentário curto do Django só vale em UMA linha: quebrado em duas, ele deixa
de ser comentário e vai LITERAL pro HTML entregue ao usuário (foi o que
aconteceu na tabela da página da hora, onde o texto ainda repetia por linha por
estar dentro de um `{% for %}`). Comentário de mais de uma linha é
`{% comment %}`.
"""

from __future__ import annotations

import re
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
_IGNORAR = (".venv", "node_modules", "staticfiles", ".git")


def _templates() -> list[Path]:
    return [
        p
        for p in _RAIZ.rglob("*.html")
        if not any(parte in p.parts for parte in _IGNORAR)
    ]


def test_sem_comentario_curto_multilinha() -> None:
    ofensores: list[str] = []
    for path in _templates():
        texto = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"\{#", texto):
            resto = texto[match.start() :]
            fim = resto.find("#}")
            if fim == -1 or "\n" in resto[:fim]:
                linha = texto[: match.start()].count("\n") + 1
                ofensores.append(f"{path.relative_to(_RAIZ)}:{linha}")
    assert not ofensores, (
        "comentário {# #} multi-linha renderiza literal no HTML; "
        f"use {{% comment %}}: {ofensores}"
    )
