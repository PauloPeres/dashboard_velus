"""OpaAtendimentoSource — implementacao de AtendimentoSourcePort para Opa! Suite.

Status Opa! -> dominio:
- F  -> CLOSED (finalizado, ~99% dos casos)
- EA -> IN_PROGRESS (em atendimento)
- A  -> OPEN (aberto)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

import structlog
from pydantic import ValidationError

from apps.atendimento.domain.dto import (
    AtendenteRefDTO,
    AtendimentoDTO,
    ClienteRefDTO,
    DepartamentoDTO,
    MensagemDTO,
)
from apps.customers.domain.services import normalize_document
from apps.integrations.shared.enums import Capability, SourceType

from .client import OpaHttpClient
from .schemas import (
    OpaAtendimentoSchema,
    OpaClienteSchema,
    OpaDepartamentoSchema,
    OpaMensagemSchema,
    OpaUsuarioSchema,
)

_logger = structlog.get_logger(__name__)

_SP_TZ = ZoneInfo("America/Sao_Paulo")

# Opa! status codes -> domain status
_STATUS_MAP = {
    "F": "CLOSED",
    "EA": "IN_PROGRESS",
    "A": "OPEN",
}


class OpaAtendimentoSource:
    """Adapter Opa! Suite para a capability ATENDIMENTO (read-only)."""

    source_type: ClassVar[SourceType] = SourceType.OPA
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.ATENDIMENTO})

    def __init__(self, *, base_url: str, token: str) -> None:
        self._client_factory = lambda: OpaHttpClient(base_url=base_url, token=token)

    # -------------------------------------------------------------------------
    # Departamentos
    # -------------------------------------------------------------------------
    def list_departamentos(self) -> Iterator[DepartamentoDTO]:
        with self._client_factory() as client:
            for raw in client.paginate_opa("departamento/"):
                try:
                    schema = OpaDepartamentoSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_departamento_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield DepartamentoDTO(
                    external_id=schema.id,
                    nome=schema.nome,
                    status=schema.status,
                    raw_extras=dict(schema.model_extra or {}),
                )

    # -------------------------------------------------------------------------
    # Clientes (mapa id_opaco -> cpf_cnpj)
    # -------------------------------------------------------------------------
    def list_clientes(self) -> Iterator[ClienteRefDTO]:
        with self._client_factory() as client:
            for raw in client.paginate_opa("cliente/"):
                try:
                    schema = OpaClienteSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_cliente_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield ClienteRefDTO(
                    external_id=schema.id,
                    document=normalize_document(schema.cpf_cnpj),
                    nome=schema.nome,
                )

    # -------------------------------------------------------------------------
    # Atendentes (mapa id_opaco -> nome)
    # -------------------------------------------------------------------------
    def list_atendentes(self) -> Iterator[AtendenteRefDTO]:
        with self._client_factory() as client:
            for raw in client.paginate_opa("usuario/"):
                try:
                    schema = OpaUsuarioSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_usuario_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield AtendenteRefDTO(external_id=schema.id, nome=schema.nome)

    # -------------------------------------------------------------------------
    # Atendimentos
    # -------------------------------------------------------------------------
    def list_atendimentos(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[AtendimentoDTO]:
        filter_ = self._build_since_filter(since) if since else None
        with self._client_factory() as client:
            skipped = 0
            for raw in client.paginate_opa("atendimento", filter=filter_):
                try:
                    schema = OpaAtendimentoSchema.model_validate(raw)
                except ValidationError as exc:
                    skipped += 1
                    _logger.warning(
                        "opa_atendimento_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield self._to_dto(schema)
            if skipped:
                _logger.info("opa_atendimento_list_done", skipped=skipped)

    def get_atendimento(self, external_id: str) -> AtendimentoDTO | None:
        with self._client_factory() as client:
            raw = client.get_one(f"atendimento/{external_id}")
        if not raw:
            return None
        try:
            schema = OpaAtendimentoSchema.model_validate(raw)
        except ValidationError:
            return None
        return self._to_dto(schema)

    # -------------------------------------------------------------------------
    # Mensagens (1 chamada por atendimento — caro)
    # -------------------------------------------------------------------------
    def list_mensagens(
        self,
        atendimento_external_id: str,
    ) -> Iterator[MensagemDTO]:
        with self._client_factory() as client:
            for raw in client.paginate_opa(
                "atendimento/mensagem",
                filter={"id_rota": atendimento_external_id},
            ):
                try:
                    schema = OpaMensagemSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_mensagem_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield MensagemDTO(
                    external_id=schema.id,
                    atendimento_external_id=schema.id_rota or atendimento_external_id,
                    direction=schema.direction,
                    tipo=schema.tipo,
                    texto=schema.mensagem,
                    sent_at=schema.data,
                    raw_extras=schema.get_extras(),
                )

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _to_dto(schema: OpaAtendimentoSchema) -> AtendimentoDTO:
        status = _STATUS_MAP.get(schema.status.upper(), "OPEN")
        return AtendimentoDTO(
            external_id=schema.id,
            customer_external_id=schema.customer_external_id,
            customer_document=normalize_document(schema.customer_document),
            customer_name=schema.customer_name,
            departamento_external_id=schema.departamento_external_id,
            atendente_external_id=schema.id_atendente,
            atendente_nome="",
            status=status,
            canal=schema.canal,
            protocol=schema.protocolo,
            opened_at=schema.date,
            motivos=schema.motivos_names,
            rating=schema.rating,
            closed_at=schema.fim,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        sp = since.astimezone(_SP_TZ)
        return {"dataInicialAbertura": sp.strftime("%Y-%m-%d")}
