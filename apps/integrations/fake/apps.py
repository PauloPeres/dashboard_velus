"""AppConfig do adapter Fake (in-memory).

Registra `FakeCustomerSource` no SourceRegistry quando o app é carregado.
Usado em testes e em ambiente de desenvolvimento sem credenciais reais.
"""

from __future__ import annotations

from django.apps import AppConfig


class FakeAdapterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.integrations.fake"
    label = "integrations_fake"
    verbose_name = "Integrations: Fake (in-memory)"

    def ready(self) -> None:
        from apps.integrations.shared.enums import Capability, SourceType
        from apps.integrations.shared.registry import registry

        from .customers import FakeCustomerSource

        # Idempotência: em reload (runserver --reload), AppConfig.ready() roda 2x.
        # Reset o registro do FAKE em runtime de dev pra permitir.
        existing = registry.get_factory(SourceType.FAKE, Capability.CUSTOMERS)
        if existing is None:
            registry.register(SourceType.FAKE, Capability.CUSTOMERS, FakeCustomerSource)
