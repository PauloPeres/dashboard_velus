"""AppConfig do servidor MCP (Model Context Protocol)."""

from __future__ import annotations

from django.apps import AppConfig


class McpConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    label = "mcp"
    verbose_name = "MCP — servidor read-only de busca de dados"
