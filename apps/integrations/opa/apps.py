"""AppConfig do adapter Opa! Suite (atendimento/WhatsApp).

Diferente do IXC, o Opa! NAO e registrado no SourceRegistry generico: ele
implementa o `AtendimentoSourcePort` (proprio do bounded context Atendimento),
nao as capabilities de sync genericas. A ingestao roda pelo comando dedicado
`sync_opasuite` — escopo read-only/analitico, sem dispatch por capability.
"""

from __future__ import annotations

from django.apps import AppConfig


class OpaAdapterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations.opa"
    label = "integrations_opa"
    verbose_name = "Integrações: Opa! Suite"
