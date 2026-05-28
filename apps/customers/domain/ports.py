"""Ports do bounded context Customers — Protocols que adapters externos implementam.

Domain define O QUE precisa, não DE QUEM vem. Implementações concretas (IXC, fake)
vivem em `apps/integrations/<source>/customers.py` e SÃO IMPORTADAS dali APENAS
pelo SourceRegistry — bounded contexts de negócio (incluindo este) nunca importam
de `apps.integrations.*`.

Convenção de assinatura:
- `list_*(since: datetime | None = None)` — incremental quando `since` setado,
  bootstrap quando None. Adapter usa pra paginar do sistema externo.
- `get_*(external_id)` — fetch único; retorna None se não existe.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import CustomerDTO


@runtime_checkable
class CustomerSourcePort(Protocol):
    """Adapter que sabe ler clientes de algum sistema externo.

    Cada implementação concreta declara:
    - `source_type: SourceType` — qual sistema (IXC, FAKE, etc.)
    - `capabilities: frozenset[Capability]` — pra registry conferir batimento

    Métodos abaixo são chamados pelo `apps.sync` orchestrator.
    """

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_customers(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[CustomerDTO]:
        """Itera clientes do sistema externo.

        - `since=None` → bootstrap (puxa tudo)
        - `since=<datetime>` → só atualizados após esse instante (incremental)

        Adapter é responsável por paginação e respeitar rate limit do sistema externo.
        Cada item é validado por Pydantic ANTES de virar CustomerDTO
        (Anti-Corruption Layer) — adapter NUNCA passa dict cru pra cá.
        """
        ...

    def get_customer(self, external_id: str) -> CustomerDTO | None:
        """Busca um cliente único pelo ID na fonte externa."""
        ...
