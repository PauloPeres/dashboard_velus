"""Envio de mensagem para o Telegram — o canal de escalonamento do NOC (P7).

**A TV é consciência contínua; o push exige ação de alguém.** É essa diferença
que decide o que passa por aqui: só o que alguém precisa largar o que está
fazendo para atender. Tudo o que for "bom saber" fica na tela.

Desligado por padrão. Sem token **e** chat configurados, nada sai — a função
registra no log e devolve `False`. Isso é deliberado: a sala não pode começar a
receber push no dia do deploy, antes de alguém decidir que quer. Mesmo idioma do
`QA_LLM_ENABLED` e do e-mail.
"""

from __future__ import annotations

import httpx
import structlog
from django.conf import settings

_logger = structlog.get_logger(__name__)

# Curto de propósito: se o Telegram não responde em 10 s, o problema é dele, e
# segurar a task de alerta não melhora nada.
_TIMEOUT_SEGUNDOS = 10.0


def enviar_telegram(texto: str, *, contexto: dict | None = None) -> bool:
    """Envia uma mensagem. Devolve True se saiu, False se estava desligado ou falhou.

    **Nunca levanta exceção.** Quem chama isto está no meio de um alerta de
    massiva: se o canal de aviso derrubar a task que detecta o problema, a falha
    de um vira a cegueira do outro.
    """
    log = _logger.bind(**(contexto or {}))
    if not getattr(settings, "TELEGRAM_ENABLED", False):
        log.info("telegram_desligado", preview=texto[:80])
        return False

    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resposta = httpx.post(
            url,
            json={
                "chat_id": settings.TELEGRAM_CHAT_ID,
                "text": texto,
                # HTML e não Markdown: nome de caixa com "_" e "*" é comum no
                # cadastro, e em Markdown isso vira formatação quebrada ou erro
                # de parse — que o Telegram devolve como 400 e some com o aviso.
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=_TIMEOUT_SEGUNDOS,
        )
    except httpx.HTTPError as exc:
        log.warning("telegram_falhou", erro=str(exc)[:200])
        return False

    if resposta.status_code >= 400:
        log.warning(
            "telegram_recusado",
            status=resposta.status_code,
            corpo=resposta.text[:200],
        )
        return False

    log.info("telegram_enviado")
    return True
