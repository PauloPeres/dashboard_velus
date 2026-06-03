"""Sobe o servidor MCP HTTP (read-only) via uvicorn.

Uso:
    python manage.py mcp_server [--host 0.0.0.0] [--port 8800]

Respeita MCP_ENABLED: se desligado, recusa subir (evita expor sem querer).
No k3s roda como Deployment próprio, exposto por Ingress.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser


class Command(BaseCommand):
    help = "Sobe o servidor MCP HTTP read-only (uvicorn)"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--host", type=str, default=settings.MCP_HOST)
        parser.add_argument("--port", type=int, default=settings.MCP_PORT)

    def handle(self, *args, **opts) -> None:
        if not settings.MCP_ENABLED:
            raise CommandError(
                "MCP_ENABLED está desligado. Defina MCP_ENABLED=true para subir o servidor."
            )

        import uvicorn

        from apps.mcp.server import build_asgi_app

        host: str = opts["host"]
        port: int = opts["port"]

        self.stdout.write(
            self.style.SUCCESS(f"Servidor MCP read-only ouvindo em {host}:{port} (/mcp)")
        )
        uvicorn.run(build_asgi_app(), host=host, port=port, log_level="info")
