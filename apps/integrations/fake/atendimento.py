"""FakeAtendimentoSource — adapter in-memory pra testes e demo."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.atendimento.domain.dto import (
    AtendimentoDTO,
    ClienteRefDTO,
    DepartamentoDTO,
    MensagemDTO,
)
from apps.integrations.shared.enums import Capability, SourceType

_seed_departamentos: list[DepartamentoDTO] = []
_seed_clientes: list[ClienteRefDTO] = []
_seed_atendimentos: list[AtendimentoDTO] = []
_seed_mensagens: dict[str, list[MensagemDTO]] = {}


class FakeAtendimentoSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.ATENDIMENTO})

    def __init__(self, **_credentials: Any) -> None:
        self._departamentos = list(_seed_departamentos)
        self._clientes = list(_seed_clientes)
        self._atendimentos = list(_seed_atendimentos)
        self._mensagens = {k: list(v) for k, v in _seed_mensagens.items()}

    # -- Seed control ---------------------------------------------------------
    @classmethod
    def set_seed(
        cls,
        *,
        departamentos: list[DepartamentoDTO] | None = None,
        clientes: list[ClienteRefDTO] | None = None,
        atendimentos: list[AtendimentoDTO] | None = None,
        mensagens: dict[str, list[MensagemDTO]] | None = None,
    ) -> None:
        global _seed_departamentos, _seed_clientes, _seed_atendimentos, _seed_mensagens
        _seed_departamentos = list(departamentos or [])
        _seed_clientes = list(clientes or [])
        _seed_atendimentos = list(atendimentos or [])
        _seed_mensagens = {k: list(v) for k, v in (mensagens or {}).items()}

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_departamentos, _seed_clientes, _seed_atendimentos, _seed_mensagens
        _seed_departamentos = []
        _seed_clientes = []
        _seed_atendimentos = []
        _seed_mensagens = {}

    # -- Port -----------------------------------------------------------------
    def list_departamentos(self) -> Iterator[DepartamentoDTO]:
        yield from self._departamentos

    def list_clientes(self) -> Iterator[ClienteRefDTO]:
        yield from self._clientes

    def list_atendimentos(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[AtendimentoDTO]:
        for dto in self._atendimentos:
            if (
                since is not None
                and dto.opened_at is not None
                and dto.opened_at < since
            ):
                continue
            yield dto

    def get_atendimento(self, external_id: str) -> AtendimentoDTO | None:
        for dto in self._atendimentos:
            if dto.external_id == external_id:
                return dto
        return None

    def list_mensagens(
        self,
        atendimento_external_id: str,
    ) -> Iterator[MensagemDTO]:
        yield from self._mensagens.get(atendimento_external_id, [])
