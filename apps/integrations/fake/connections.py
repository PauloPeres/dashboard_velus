"""FakeConnectionSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import ConnectionDTO

_seed_connections: list[ConnectionDTO] = []
# Falha injetada na leitura de status. Existe porque a tolerância a falha
# transiente do IXC é requisito do poll (#144), e requisito sem teste é promessa:
# o host do IXC é dual-stack sem rota IPv6 no cluster e a API às vezes devolve
# uma página HTML de erro em vez de JSON.
_offline_failure: Exception | None = None


class FakeConnectionSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.CONNECTIONS})

    def __init__(self, **_credentials: Any) -> None:
        self._connections: list[ConnectionDTO] = list(_seed_connections)
        self._failure = _offline_failure

    @classmethod
    def set_seed(cls, connections: list[ConnectionDTO]) -> None:
        global _seed_connections
        _seed_connections = list(connections)

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_connections, _offline_failure
        _seed_connections = []
        _offline_failure = None

    @classmethod
    def fail_offline_listing(cls, error: Exception | None) -> None:
        """Faz a próxima leitura de offline levantar `error` (None desliga)."""
        global _offline_failure
        _offline_failure = error

    def list_connections(
        self, *, since: datetime | None = None
    ) -> Iterator[ConnectionDTO]:
        for dto in self._connections:
            if (
                since is not None
                and dto.last_connection_at is not None
                and dto.last_connection_at < since
            ):
                continue
            yield dto

    def list_offline_connections(self) -> Iterator[ConnectionDTO]:
        if self._failure is not None:
            raise self._failure
        for dto in self._connections:
            # Espelha o filtro do IXC (`online=N & ativo=S`): quem está no ar,
            # bloqueado ou sem sessão registrada não aparece nesta lista.
            if dto.status == "OFFLINE":
                yield dto

    def get_connection(self, external_id: str) -> ConnectionDTO | None:
        for dto in self._connections:
            if dto.external_id == external_id:
                return dto
        return None
