"""OpaAtendimentoSource — implementacao de AtendimentoSourcePort para Opa! Suite.

Status Opa! -> dominio:
- F  -> CLOSED (finalizado, ~99% dos casos)
- EA -> IN_PROGRESS (em atendimento)
- A  -> OPEN (aberto)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

import structlog
from pydantic import ValidationError

from apps.atendimento.domain.dto import (
    AtendenteRefDTO,
    AtendimentoDTO,
    CanalComunicacaoDTO,
    ClienteRefDTO,
    DepartamentoDTO,
    EtiquetaDTO,
    MensagemDTO,
    MotivoDTO,
)
from apps.customers.domain.services import normalize_document
from apps.integrations.shared.enums import Capability, SourceType

from .client import OpaHttpClient
from .schemas import (
    OpaAtendimentoSchema,
    OpaCanalSchema,
    OpaClienteSchema,
    OpaDepartamentoSchema,
    OpaEtiquetaSchema,
    OpaMensagemSchema,
    OpaMotivoSchema,
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
    # Etiquetas (catalogo id_tag -> nome)
    # -------------------------------------------------------------------------
    def list_etiquetas(self) -> Iterator[EtiquetaDTO]:
        with self._client_factory() as client:
            for raw in client.paginate_opa("etiqueta/"):
                try:
                    schema = OpaEtiquetaSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_etiqueta_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield EtiquetaDTO(
                    external_id=schema.id,
                    nome=schema.nome,
                    cor=schema.cor,
                    raw_extras=dict(schema.model_extra or {}),
                )

    # -------------------------------------------------------------------------
    # Motivos (catalogo idMotivo -> nome)
    # -------------------------------------------------------------------------
    def list_motivos(self) -> Iterator[MotivoDTO]:
        with self._client_factory() as client:
            for raw in client.paginate_opa("atendimento/motivo"):
                try:
                    schema = OpaMotivoSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_motivo_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield MotivoDTO(
                    external_id=schema.id,
                    nome=schema.nome,
                    raw_extras=dict(schema.model_extra or {}),
                )

    # -------------------------------------------------------------------------
    # Canais de comunicacao (catalogo id -> nome/midia/integracao)
    # -------------------------------------------------------------------------
    def list_canais(self) -> Iterator[CanalComunicacaoDTO]:
        """Itera o catalogo de canais/numeros configurados (barato, ~dezenas)."""
        with self._client_factory() as client:
            for raw in client.paginate_opa("canal-comunicacao"):
                try:
                    schema = OpaCanalSchema.model_validate(raw)
                except ValidationError as exc:
                    _logger.warning(
                        "opa_canal_schema_invalid_skipped",
                        external_id=raw.get("_id"),
                        errors=exc.errors()[:1],
                    )
                    continue
                yield CanalComunicacaoDTO(
                    external_id=schema.id,
                    nome=schema.nome,
                    canal=schema.canal,
                    integracao=schema.integracao,
                    status=schema.status,
                    raw_extras=schema.get_extras(),
                )

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
                    canal_external_id=schema.canalComunicacao,
                    fora_janela_24h=schema.envioForaJanela24h,
                    raw_extras=schema.get_extras(),
                )

    # -------------------------------------------------------------------------
    # Mensagens em massa (listagem global — barata)
    # -------------------------------------------------------------------------
    def list_mensagens_global(
        self,
        *,
        start_skip: int = 0,
        page_size: int = 100,
        max_pages: int | None = None,
    ) -> Iterator[tuple[int, MensagemDTO]]:
        """Itera TODAS as mensagens da conta, da mais antiga pra mais nova.

        `atendimento/mensagem` aceita listagem sem `id_rota` — a API ignora
        qualquer outro filtro (testado: data, canal, tipo e `sort` nao surtem
        efeito), mas a ordem e a de insercao e a colecao e append-only, entao
        paginar por `skip` e estavel: um registro novo nunca se insere no meio e
        desloca o cursor. Por isso o backfill de volume nao precisa das ~34 mil
        chamadas de `list_mensagens` (1 por atendimento) — sao ~6,7 mil pra
        historia inteira de 2026, e ~30/dia no incremental.

        Yield `(offset_absoluto, dto)`: o offset e o checkpoint que a proxima
        rodada retoma, e so ele sobrevive a uma interrupcao no meio.
        """
        skip = start_skip
        pages = 0
        with self._client_factory() as client:
            while max_pages is None or pages < max_pages:
                body = {"filter": {}, "options": {"limit": page_size, "skip": skip}}
                response = client.get("atendimento/mensagem", json=body)
                items = response.get("data") if isinstance(response, dict) else None
                if not items:
                    return
                for offset, raw in enumerate(items, start=skip):
                    try:
                        schema = OpaMensagemSchema.model_validate(raw)
                    except ValidationError as exc:
                        _logger.warning(
                            "opa_mensagem_schema_invalid_skipped",
                            external_id=raw.get("_id"),
                            errors=exc.errors()[:1],
                        )
                        continue
                    yield offset, MensagemDTO(
                        external_id=schema.id,
                        atendimento_external_id=schema.id_rota,
                        direction=schema.direction,
                        tipo=schema.tipo,
                        # Backfill de volume nao guarda texto (PII sem uso).
                        texto="",
                        sent_at=schema.data,
                        canal_external_id=schema.canalComunicacao,
                        fora_janela_24h=schema.envioForaJanela24h,
                        raw_extras={},
                    )
                pages += 1
                skip += len(items)
                if len(items) < page_size:
                    return

    def find_skip_for_date(self, target: datetime, *, ceiling: int = 4_000_000) -> int:
        """Menor `skip` cuja mensagem ja e >= `target` (busca binaria).

        A listagem global nao aceita filtro de data, mas e ordenada por
        insercao — entao da pra "procurar" a data por bissecao em ~22 chamadas
        em vez de varrer 2,8 milhoes de registros ate chegar em 2026.
        """
        alvo = target.astimezone(UTC)
        lo, hi = 0, ceiling
        with self._client_factory() as client:
            while lo < hi:
                mid = (lo + hi) // 2
                body = {"filter": {}, "options": {"limit": 1, "skip": mid}}
                response = client.get("atendimento/mensagem", json=body)
                items = response.get("data") if isinstance(response, dict) else None
                if not items:
                    # Passou do fim da colecao — a data procurada esta atras.
                    hi = mid
                    continue
                try:
                    data = OpaMensagemSchema.model_validate(items[0]).data
                except ValidationError:
                    data = None
                if data is None or data.astimezone(UTC) >= alvo:
                    hi = mid
                else:
                    lo = mid + 1
        return lo

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _to_dto(schema: OpaAtendimentoSchema) -> AtendimentoDTO:
        status = _STATUS_MAP.get(schema.status.upper(), "OPEN")
        return AtendimentoDTO(
            canal_external_id=schema.canal_external_id,
            origem_tipo=schema.origem_tipo,
            origem_ref=schema.origem_ref,
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
            motivo_ids=schema.motivo_ids,
            tag_ids=schema.tag_ids,
            rating=schema.rating,
            closed_at=schema.fim,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _build_since_filter(since: datetime) -> dict[str, str]:
        sp = since.astimezone(_SP_TZ)
        return {"dataInicialAbertura": sp.strftime("%Y-%m-%d")}
