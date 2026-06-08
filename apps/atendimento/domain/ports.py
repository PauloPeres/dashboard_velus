"""Ports do bounded context Atendimento — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import AtendimentoDTO, ClienteRefDTO, DepartamentoDTO, MensagemDTO


@runtime_checkable
class AtendimentoSourcePort(Protocol):
    """Adapter que sabe ler atendimentos/conversas de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_departamentos(self) -> Iterator[DepartamentoDTO]:
        """Itera os departamentos/setores de atendimento."""
        ...

    def list_clientes(self) -> Iterator[ClienteRefDTO]:
        """Itera referencias de cliente (id opaco -> documento).

        Lista barata usada pra montar o mapa que liga atendimento -> Customer
        sem precisar popular cada atendimento individualmente.
        """
        ...

    def list_atendimentos(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[AtendimentoDTO]:
        """Itera atendimentos. since=None -> bootstrap; senao incremental."""
        ...

    def get_atendimento(self, external_id: str) -> AtendimentoDTO | None:
        """Busca atendimento unico (GET populado — traz rating/avaliacao)."""
        ...

    def list_mensagens(
        self,
        atendimento_external_id: str,
    ) -> Iterator[MensagemDTO]:
        """Itera as mensagens de um atendimento (1 chamada por atendimento — caro)."""
        ...
