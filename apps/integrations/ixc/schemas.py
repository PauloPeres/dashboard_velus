"""Schemas Pydantic dos endpoints IXC.

Anti-Corruption Layer (AGENT.md §1.6 #3): toda resposta IXC vira primeiro
um schema validado AQUI, antes de virar DTO. Se o IXC mudar schema entre
updates do ERP, o erro fica contido (AdapterContractError) — não corrompe
fact tables nem propaga lixo pro domain.

Campos `Optional` cobrem variabilidade comum nas instalações IXC. Campos
desconhecidos passam por `extras` (config `extra='allow'`) e vão pra
`raw_extras` do DTO.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_str(v: Any) -> str:
    """IXC ora retorna int, ora string. Normaliza pra string."""
    return str(v) if v is not None else ""


class IxcCustomerSchema(BaseModel):
    """Schema do registro `cliente` na API IXC.

    Campos confirmados a partir da Postman collection + projeto antigo de
    migração ONU. Campos opcionais quando o IXC pode omiti-los em instalações
    minimalistas.
    """

    model_config = ConfigDict(
        extra="allow",  # campos não mapeados ficam em `model_extra`
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    id: str = Field(...)
    razao: str = Field(...)
    cnpj_cpf: str = Field(default="")
    email: str | None = Field(default=None)
    telefone_celular: str | None = Field(default=None)
    ativo: str = Field(default="S")  # IXC usa "S"/"N"
    data_cadastro: datetime | None = Field(default=None)

    @field_validator("id", "razao", "cnpj_cpf", "ativo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("email", "telefone_celular", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return None
        return v

    @field_validator("data_cadastro", mode="before")
    @classmethod
    def _parse_ixc_datetime(cls, v: Any) -> datetime | None:
        """IXC retorna `YYYY-MM-DD HH:MM:SS` ou `YYYY-MM-DD`, sem tz.

        Convertemos pra UTC-aware — sistema do cliente roda America/Sao_Paulo,
        mas analytics agrega em UTC. Adapter assume que data_cadastro está em
        horário local (BRT/BRST) — para precisão maior, ajustar com pytz/zoneinfo.
        """
        if v in (None, "", "0000-00-00 00:00:00", "0000-00-00"):
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            from zoneinfo import ZoneInfo

            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    naive = datetime.strptime(v, fmt)
                    # IXC roda em horário local do cliente; assumimos São Paulo.
                    return naive.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
                except ValueError:
                    continue
        return None

    @property
    def is_active(self) -> bool:
        return self.ativo.upper() == "S"

    def get_extras(self) -> dict[str, Any]:
        """Campos não mapeados — vão pra `CustomerDTO.raw_extras`."""
        return dict(self.model_extra or {})


# =============================================================================
# Contrato — endpoint /contrato no IXC
# =============================================================================
class IxcContractSchema(BaseModel):
    """Schema do registro `contrato` na API IXC.

    Campos confirmados a partir de docs do IXC + projeto antigo. Status do
    contrato no IXC vem em campos como `status_internet`/`status_contrato`,
    com valores como A (Ativo), B (Bloqueado), CA (Cancelado), AA (Aguardando).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_cliente: str = Field(...)
    id_vd_contrato: str = Field(default="")  # nome do plano frequentemente vem em descricao
    descricao_plano: str | None = Field(default=None)
    mensalidade: str = Field(default="0")  # vem string formato "150.00"
    status: str = Field(default="A")
    status_internet: str | None = Field(default=None)
    data_ativacao: datetime | None = Field(default=None)
    data_cancelamento: datetime | None = Field(default=None)
    endereco: str | None = Field(default=None)

    @field_validator("id", "id_cliente", "status", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("descricao_plano", "status_internet", "endereco", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return None
        return v

    @field_validator("mensalidade", mode="before")
    @classmethod
    def _coerce_amount_str(cls, v: Any) -> str:
        if v is None:
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_ativacao", "data_cancelamento", mode="before")
    @classmethod
    def _parse_ixc_datetime(cls, v: Any) -> datetime | None:
        if v in (None, "", "0000-00-00 00:00:00", "0000-00-00"):
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            from zoneinfo import ZoneInfo
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    naive = datetime.strptime(v, fmt)
                    return naive.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
                except ValueError:
                    continue
        return None

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


# =============================================================================
# Fatura — endpoint /fn no IXC (financeiro_cliente)
# =============================================================================
class IxcInvoiceSchema(BaseModel):
    """Schema do registro `fn` (financeiro_cliente) na API IXC.

    IXC retorna boletos com status: A (aberto), R (recebido/pago), C (cancelado),
    AT (atraso). Datas são strings no formato brasileiro.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_cliente: str = Field(default="")
    id_contrato: str = Field(default="")
    valor: str = Field(default="0")
    data_vencimento: str = Field(default="")  # YYYY-MM-DD
    data_emissao: datetime | None = Field(default=None)
    data_pgto: datetime | None = Field(default=None)
    valor_pago: str | None = Field(default=None)
    status: str = Field(default="A")

    @field_validator("id", "id_cliente", "id_contrato", "status", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", "valor_pago", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str | None:
        if v in (None, "", "0.00"):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_emissao", "data_pgto", mode="before")
    @classmethod
    def _parse_ixc_datetime(cls, v: Any) -> datetime | None:
        if v in (None, "", "0000-00-00 00:00:00", "0000-00-00"):
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            from zoneinfo import ZoneInfo
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    naive = datetime.strptime(v, fmt)
                    return naive.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
                except ValueError:
                    continue
        return None

    @field_validator("data_vencimento", mode="before")
    @classmethod
    def _coerce_due_date(cls, v: Any) -> str:
        if v in (None, "", "0000-00-00"):
            return ""
        if isinstance(v, str):
            return v[:10]  # YYYY-MM-DD
        return str(v)

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})
