"""Pareamento de TV e credencial de display — comum a todos os painéis.

Digitar uma URL com token numa TV é inviável, então o fluxo é o mesmo que
Netflix e YouTube usam (*device authorization grant*), adaptado:

1. a TV abre a tela de pareamento e recebe um **código curto** e um `device_id`
   próprio;
2. a tela mostra o QR (e o código por extenso, para quem preferir digitar);
3. alguém escaneia com o celular, **faz login normalmente** e aprova aquela TV;
4. a TV, que está consultando o status, recebe a **credencial de display**.

Dois cuidados que não podem sair daqui, ou o fluxo vira buraco (§1 do
`docs/massivas-painel-tv-plano.md`):

- **o QR carrega o código de pareamento, nunca a credencial.** Quem fotografa a
  tela da TV não ganha acesso: a credencial é emitida para o navegador que
  iniciou o pedido, e só ele a recebe, pela consulta de status autenticada pelo
  `device_id` secreto que nunca aparece na tela;
- **o código é efêmero e de uso único**, e a credencial resultante é
  **revogável** — a lista de dispositivos pareados existe para derrubar uma TV
  que sumiu, foi roubada ou mudou de sala.

A credencial guardada é um **hash**: quem lê o banco não consegue abrir o
painel, e um vazamento de dump não vira TV alheia no ar.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from django.utils import timezone

# Alfabeto sem 0/O/1/I/5/S: o código é lido de longe, numa TV, e ditado por
# telefone. Ambiguidade aqui vira "não consigo parear" na sala de operação.
_ALFABETO = "ABCDEFGHJKLMNPQRTUVWXYZ2346789"
CODIGO_TAMANHO = 6

# O código morre rápido de propósito: ele é a única coisa que aparece na tela, e
# uma tela de TV fica exposta a quem passa na sala.
CODIGO_VALIDADE = timedelta(minutes=5)

# A credencial de display não expira sozinha — a TV fica ligada meses, e uma
# expiração silenciosa viraria painel apagado de madrugada sem ninguém para
# reparear. Ela morre por revogação explícita, que é uma decisão de quem
# administra, não um efeito colateral do relógio.
COOKIE_NOME = "velus_display"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def novo_codigo() -> str:
    """Código curto de pareamento, legível a 4 metros."""
    return "".join(secrets.choice(_ALFABETO) for _ in range(CODIGO_TAMANHO))


def novo_segredo() -> str:
    """Segredo longo — `device_id` da TV ou credencial de display."""
    return secrets.token_urlsafe(32)


def hash_segredo(valor: str) -> str:
    """Hash do que vai para o banco.

    SHA-256 simples, e não hash de senha: o segredo tem 256 bits de entropia
    gerada por nós, então não há dicionário a proteger — o custo de um KDF aqui
    só encareceria cada carregamento do painel.
    """
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()


def codigo_expirou(criado_em: Any, *, agora: Any = None) -> bool:
    agora = agora or timezone.now()
    return (agora - criado_em) > CODIGO_VALIDADE
