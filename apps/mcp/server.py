"""Servidor MCP HTTP read-only com autenticação Bearer por organização.

Arquitetura:
- FastMCP expõe as ferramentas do `registry` via transporte streamable-http.
- Um middleware ASGI puro intercepta cada request: lê o header
  `Authorization: Bearer <token>`, resolve a organização dona do token e a
  injeta no contextvar de tenant ANTES de despachar para o app MCP.
- As ferramentas do registry são funções SÍNCRONAS (puras, testáveis). Como o
  FastMCP chama tools inline no event loop, cada handler é adaptado com
  `sync_to_async` — o asgiref copia o contexto (org incluída) para a thread.

Sem token válido → 401. O isolamento multi-tenant é estrutural: a tool nunca
recebe a org como parâmetro, ela vem sempre do token autenticado.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from asgiref.sync import sync_to_async

from apps.shared.context import reset_current_organization, set_current_organization

_HEALTH_PATH = "/healthz"


def _transport_security():
    """Política anti DNS-rebinding do FastMCP a partir de MCP_ALLOWED_HOSTS.

    Lista vazia → proteção desligada (dev/local, onde o Host é localhost:porta).
    Lista preenchida → proteção ligada, aceitando os hosts configurados (mais a
    variação com porta) — o header Host atrás do Ingress é o domínio público.
    """
    from django.conf import settings
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = list(settings.MCP_ALLOWED_HOSTS)
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    allowed = []
    for h in hosts:
        allowed.append(h)
        if ":" not in h:
            allowed.append(f"{h}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=allowed,
    )


def _build_mcp():
    """Constrói o FastMCP e registra todas as ferramentas do catálogo."""
    from mcp.server.fastmcp import FastMCP

    from .registry import TOOL_SPECS

    mcp = FastMCP(
        name="velus-data",
        instructions=(
            "Servidor read-only de dados do dashboard Velus (ISP). Todas as "
            "ferramentas operam sobre a organização do token autenticado. "
            "Use para consultar MRR, churn, caixa, inadimplência e risco."
        ),
        stateless_http=True,
        transport_security=_transport_security(),
    )

    for spec in TOOL_SPECS:
        mcp.add_tool(
            _as_async(spec.handler, spec.months_param, spec.limit_param),
            name=spec.name,
            description=spec.description,
        )

    return mcp


def _as_async(
    handler: Callable[..., Any], months_param: bool, limit_param: bool
) -> Callable[..., Awaitable[Any]]:
    """Adapta um handler síncrono para coroutine com a assinatura certa.

    A assinatura exposta define o input-schema da tool no MCP, por isso há uma
    variação por tipo de parâmetro (meses, limite, nenhum).
    """
    run = sync_to_async(handler, thread_sensitive=True)

    if months_param:

        async def _months(months: int = 12) -> Any:
            return await run(months=months)

        return _months

    if limit_param:

        async def _limit(limit: int = 20) -> Any:
            return await run(limit=limit)

        return _limit

    async def _plain() -> Any:
        return await run()

    return _plain


async def _send_json(send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BearerTenantMiddleware:
    """ASGI middleware: Bearer token → organização no contextvar de tenant."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == _HEALTH_PATH:
            await _send_json(send, 200, {"status": "ok"})
            return

        from .models import authenticate_token

        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else None

        organization = await sync_to_async(authenticate_token, thread_sensitive=True)(raw)
        if organization is None:
            await _send_json(
                send, 401, {"error": "unauthorized", "detail": "Token MCP inválido."}
            )
            return

        token = set_current_organization(organization)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_organization(token)


def build_asgi_app():
    """App ASGI completo: middleware de auth + transporte streamable-http."""
    mcp = _build_mcp()
    return BearerTenantMiddleware(mcp.streamable_http_app())
