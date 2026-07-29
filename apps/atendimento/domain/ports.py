"""Ports do bounded context Atendimento — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import (
    AtendenteRefDTO,
    AtendimentoDTO,
    ClienteRefDTO,
    DepartamentoDTO,
    EtiquetaDTO,
    MensagemDTO,
    MotivoDTO,
)


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

    def list_atendentes(self) -> Iterator[AtendenteRefDTO]:
        """Itera referencias de atendente (id opaco -> nome).

        Lista barata usada pra preencher o nome do atendente no atendimento,
        que a listagem so traz como id opaco.
        """
        ...

    def list_etiquetas(self) -> Iterator[EtiquetaDTO]:
        """Itera o catalogo de etiquetas/tags configuradas (id opaco -> nome).

        Lista barata usada pra resolver `Atendimento.tags`: a listagem de
        atendimentos so traz as tags como ids opacos (id_tag), sem o nome.
        """
        ...

    def list_motivos(self) -> Iterator[MotivoDTO]:
        """Itera o catalogo de motivos configurados (id opaco -> nome).

        Lista barata usada pra resolver `Atendimento.motivos`: a listagem de
        atendimentos so traz os motivos como ids opacos (idMotivo), sem o nome.
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
