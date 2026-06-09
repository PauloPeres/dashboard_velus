"""Schemas Pydantic dos endpoints Opa! Suite.

Anti-Corruption Layer: toda resposta Opa! vira primeiro um schema validado AQUI
antes de virar DTO. Se a Opa! mudar o schema, o erro fica contido
(ValidationError -> skip do registro) e nao corrompe o dominio.

Campos polimorficos: `id_cliente` e `setor` vem como id opaco (string) na
listagem, mas como objeto populado (`{_id, nome, cpf_cnpj, ...}`) no GET unitario.
Os validators normalizam ambos os formatos.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


def _to_str(v: Any) -> str:
    return str(v) if v is not None else ""


def _id_of(v: Any) -> str:
    """Extrai o id de um campo que pode ser string (id opaco) ou dict populado."""
    if isinstance(v, dict):
        return _to_str(v.get("_id") or v.get("id"))
    return _to_str(v)


def _parse_dt(v: Any) -> datetime | None:
    if v in (None, "", "null"):
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            # ISO 8601 Mongo: "2023-07-05T12:00:00.000Z"
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class _OpaBase(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class OpaDepartamentoSchema(_OpaBase):
    """Registro de `departamento` (setor de atendimento)."""

    id: str = Field(validation_alias=AliasChoices("_id", "id"))
    nome: str = Field(default="")
    status: str = Field(default="")

    @field_validator("id", "nome", "status", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)


class OpaClienteSchema(_OpaBase):
    """Registro de `cliente` — usado pra montar o mapa id_opaco -> cpf_cnpj."""

    id: str = Field(validation_alias=AliasChoices("_id", "id"))
    nome: str = Field(default="")
    cpf_cnpj: str = Field(default="")

    @field_validator("id", "nome", "cpf_cnpj", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)


class OpaUsuarioSchema(_OpaBase):
    """Registro de `usuario` (atendente/operador) — mapa id opaco -> nome."""

    id: str = Field(validation_alias=AliasChoices("_id", "id"))
    nome: str = Field(default="")

    @field_validator("id", "nome", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)


class OpaAtendimentoSchema(_OpaBase):
    """Registro de `atendimento` (conversa/chamado omnichannel).

    `id_cliente`/`setor` polimorficos (id ou objeto populado). `evaluations` so
    vem populado no GET unitario — na listagem chega vazio.
    """

    id: str = Field(validation_alias=AliasChoices("_id", "id"))
    id_cliente: Any = Field(default=None)
    id_atendente: str = Field(default="")
    setor: Any = Field(default=None)
    status: str = Field(default="")
    canal: str = Field(default="")
    protocolo: str = Field(default="")
    motivos: list[Any] = Field(default_factory=list)
    evaluations: list[Any] = Field(default_factory=list)
    date: datetime | None = Field(default=None)
    fim: datetime | None = Field(default=None)

    @field_validator("id", "id_atendente", "status", "canal", "protocolo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("motivos", "evaluations", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> list[Any]:
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple)) else [v]

    @field_validator("date", "fim", mode="before")
    @classmethod
    def _coerce_dt(cls, v: Any) -> datetime | None:
        return _parse_dt(v)

    # -- Derivados ------------------------------------------------------------
    @property
    def customer_external_id(self) -> str:
        return _id_of(self.id_cliente)

    @property
    def customer_document(self) -> str:
        """So presente quando id_cliente vem populado (GET unitario)."""
        if isinstance(self.id_cliente, dict):
            return _to_str(self.id_cliente.get("cpf_cnpj"))
        return ""

    @property
    def customer_name(self) -> str:
        if isinstance(self.id_cliente, dict):
            return _to_str(self.id_cliente.get("nome"))
        return ""

    @property
    def departamento_external_id(self) -> str:
        return _id_of(self.setor)

    @property
    def motivos_names(self) -> list[str]:
        out: list[str] = []
        for m in self.motivos:
            if isinstance(m, dict):
                out.append(_to_str(m.get("nome") or m.get("descricao") or m.get("_id")))
            else:
                out.append(_to_str(m))
        return [x for x in out if x]

    @property
    def rating(self) -> int | None:
        """Primeira nota likert 1-5 das evaluations (so vem em GET populado)."""
        for ev in self.evaluations:
            if not isinstance(ev, dict):
                continue
            likert = ev.get("likert")
            if isinstance(likert, dict) and likert.get("rating") is not None:
                try:
                    return int(likert["rating"])
                except (TypeError, ValueError):
                    continue
        return None

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class OpaMensagemSchema(_OpaBase):
    """Registro de `mensagem` de atendimento.

    `tipoDestinatario` e o tipo de quem RECEBE a mensagem (o destinatario),
    nao de quem envia: `usuarios` = entregue a um atendente (logo ENVIADA pelo
    cliente), `clientes_users` = entregue ao cliente (logo ENVIADA pelo
    atendente/bot). Ver `direction`.
    """

    id: str = Field(validation_alias=AliasChoices("_id", "id"))
    id_rota: str = Field(default="")
    mensagem: str = Field(default="")
    tipo: str = Field(default="")
    tipoDestinatario: str = Field(default="", validation_alias=AliasChoices("tipoDestinatario", "tipo_destinatario"))
    data: datetime | None = Field(default=None)

    @field_validator("id", "id_rota", "mensagem", "tipo", "tipoDestinatario", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_dt(cls, v: Any) -> datetime | None:
        return _parse_dt(v)

    @property
    def direction(self) -> str:
        # tipoDestinatario e o DESTINATARIO (quem recebe), entao o autor e o
        # oposto: destinatario `usuarios` (atendente) => ENVIADA pelo cliente;
        # destinatario `clientes_users` (cliente) => ENVIADA pelo atendente/bot.
        dest = (self.tipoDestinatario or "").lower()
        if dest == "usuarios":
            return "CLIENT"
        if dest == "clientes_users":
            return "AGENT"
        return "SYSTEM"

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})
