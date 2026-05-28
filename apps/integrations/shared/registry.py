"""SourceRegistry — mapeamento (SourceType, Capability) → factory de adapter.

Singleton global. Adapters registram-se em `AppConfig.ready()` do seu próprio
app — registro explícito, sem auto-import-walk (mágico e difícil de debugar).

Uso:
    # No apps/integrations/ixc/apps.py:
    class IxcConfig(AppConfig):
        name = "apps.integrations.ixc"

        def ready(self):
            from .customers import IxcCustomerSource
            from apps.integrations.shared.registry import registry
            from apps.integrations.shared.enums import SourceType, Capability

            registry.register(SourceType.IXC, Capability.CUSTOMERS, IxcCustomerSource)

    # No apps/sync/tasks.py:
    from apps.integrations.shared.registry import registry

    sources = registry.get_sources(organization, Capability.CUSTOMERS)
    for source in sources:
        for dto in source.list_customers(since=last_sync):
            repository.upsert(dto, source_type=source.source_type)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .enums import Capability, SourceType

if TYPE_CHECKING:
    from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)


class SourceRegistry:
    """Mantém o mapeamento (SourceType, Capability) → classe de adapter.

    Re-registro com mesma chave levanta erro — duplicação é bug, não feature.
    `reset()` existe pra testes que precisam reconfigurar.
    """

    def __init__(self) -> None:
        # Aninhado por SourceType pra permitir lookup hierárquico se necessário.
        # {SourceType: {Capability: AdapterClass}}
        self._factories: dict[SourceType, dict[Capability, type]] = {}

    def register(
        self,
        source: SourceType,
        capability: Capability,
        adapter_cls: type,
    ) -> None:
        """Registra um adapter pra um (SourceType, Capability)."""
        per_source = self._factories.setdefault(source, {})
        if capability in per_source:
            existing = per_source[capability]
            raise RuntimeError(
                f"Adapter já registrado: {source.value}/{capability.value} = "
                f"{existing.__name__}; tentativa de re-registro com {adapter_cls.__name__}"
            )
        per_source[capability] = adapter_cls
        _logger.info(
            "adapter_registered",
            source=source.value,
            capability=capability.value,
            adapter=adapter_cls.__name__,
        )

    def get_factory(
        self,
        source: SourceType,
        capability: Capability,
    ) -> type | None:
        """Retorna a classe do adapter pra (source, capability), ou None."""
        return self._factories.get(source, {}).get(capability)

    def get_sources(
        self,
        organization: Organization,
        capability: Capability,
    ) -> list[Any]:
        """Resolve OrganizationDataSource → adapters concretos prontos pra uso.

        Lista ordenada por `priority` decrescente. Cada adapter é instanciado
        com credenciais descriptografadas (kwargs do __init__).

        Se um adapter está configurado em OrganizationDataSource mas não
        registrado, é pulado com warning estruturado (auto-recuperação) — útil
        em deploys staged onde nem todos adapters estão deployados ainda.
        """
        # Import local pra evitar circular: tenancy → shared → tenancy
        from apps.tenancy.models import OrganizationDataSource

        configs = (
            OrganizationDataSource.objects
            .filter(
                organization=organization,
                capability=capability.value,
                is_active=True,
            )
            .order_by("-priority")
        )

        instances: list[Any] = []
        for cfg in configs:
            try:
                source_type = SourceType(cfg.source_type)
            except ValueError:
                _logger.warning(
                    "unknown_source_type_in_db",
                    source=cfg.source_type,
                    organization=organization.slug,
                )
                continue

            factory = self.get_factory(source_type, capability)
            if factory is None:
                _logger.warning(
                    "adapter_not_registered",
                    source=source_type.value,
                    capability=capability.value,
                    organization=organization.slug,
                    hint="Adapter pode estar fora de INSTALLED_APPS ou ready() não rodou.",
                )
                continue

            credentials = cfg.get_credentials()
            try:
                instance = factory(**credentials)
            except TypeError as exc:
                _logger.error(
                    "adapter_construction_failed",
                    adapter=factory.__name__,
                    organization=organization.slug,
                    error=str(exc),
                    hint="Credenciais no DB não batem com __init__ do adapter.",
                )
                continue
            instances.append(instance)

        return instances

    def list_registered(self) -> dict[str, list[str]]:
        """Útil em management command de health check.

        Retorna `{source_type: [capability, ...]}`.
        """
        return {
            source.value: [cap.value for cap in caps]
            for source, caps in self._factories.items()
        }

    def reset(self) -> None:
        """Limpa todo registro — uso EXCLUSIVO em testes."""
        self._factories.clear()


# Singleton global. Adapters acessam `registry` deste módulo pra registrar.
registry = SourceRegistry()
