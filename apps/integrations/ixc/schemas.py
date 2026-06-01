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
    # IXC usa pagamento_data/pagamento_valor para data e valor efetivos de pagamento.
    # O campo data_pgto (legado) existe na API mas geralmente vem null — preferir pagamento_data.
    pagamento_data: str | None = Field(default=None)  # "YYYY-MM-DD" quando pago
    pagamento_valor: str | None = Field(default=None)  # valor recebido
    valor_recebido: str | None = Field(default=None)   # alias de pagamento_valor (alguns endpoints)
    data_pgto: datetime | None = Field(default=None)   # legado — geralmente null no IXC
    valor_pago: str | None = Field(default=None)
    status: str = Field(default="A")

    @field_validator("id", "id_cliente", "id_contrato", "status", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", "valor_pago", "pagamento_valor", "valor_recebido", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str | None:
        if v in (None, "", "0.00"):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("pagamento_data", mode="before")
    @classmethod
    def _coerce_pagamento_data(cls, v: Any) -> str | None:
        if v in (None, "", "0000-00-00"):
            return None
        return str(v)[:10]  # keep only YYYY-MM-DD portion

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


# =============================================================================
# Plano de venda — endpoint /vd_contratos no IXC
# =============================================================================
class IxcPlanSchema(BaseModel):
    """Schema do registro `vd_contratos` (Planos de venda) na API IXC.

    Usado por IxcContractSource pra enriquecer o ContractDTO com nome do plano
    e mensalidade — `cliente_contrato` só guarda a FK `id_vd_contrato`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    nome: str = Field(default="")
    descricao: str | None = Field(default=None)
    valor_contrato: str = Field(default="0")
    comissao: str = Field(default="0")
    fidelidade: str = Field(default="0")  # meses
    tipo_pessoa: str = Field(default="")
    moeda: str = Field(default="R$")

    @field_validator("id", "nome", "comissao", "fidelidade", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("descricao", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        if v in (None, "", "null"):
            return None
        return v

    @field_validator("valor_contrato", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v is None or v == "":
            return "0"
        return str(v).replace(",", ".")


# =============================================================================
# Despesa — endpoint /fn_apagar no IXC (contas a pagar)
# =============================================================================
class IxcExpenseSchema(BaseModel):
    """Schema do registro `fn_apagar` (contas a pagar) na API IXC.

    Status: F=Pago, A=Aberto, C=Cancelado.
    Datas podem ser "" em vez de null — validadores normalizam pra "".
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_fornecedor: str = Field(default="")
    valor: str = Field(default="0")
    valor_pago: str = Field(default="0")
    valor_aberto: str = Field(default="0")
    data_emissao: str = Field(default="")    # YYYY-MM-DD ou ""
    data_vencimento: str = Field(default="")  # YYYY-MM-DD
    data_pagamento: str = Field(default="")   # YYYY-MM-DD ou ""
    status: str = Field(default="A")          # F=pago, A=aberto, C=cancelado
    tipo_pagamento: str = Field(default="")
    obs: str = Field(default="")

    @field_validator("id", "id_fornecedor", "status", "tipo_pagamento", "obs", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", "valor_pago", "valor_aberto", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, ""):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_emissao", "data_vencimento", "data_pagamento", mode="before")
    @classmethod
    def _coerce_date_str(cls, v: Any) -> str:
        if v in (None, "", "0000-00-00", "0000-00-00 00:00:00"):
            return ""
        s = str(v)
        return s[:10]  # YYYY-MM-DD

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


# =============================================================================
# Fornecedor — endpoint /fornecedor no IXC
# =============================================================================
class IxcSupplierSchema(BaseModel):
    """Schema do registro `fornecedor` na API IXC."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    fantasia: str = Field(default="")
    razao: str = Field(default="")  # razão social — usado como fallback quando fantasia está vazio
    cpf_cnpj: str = Field(default="")
    ativo: str = Field(default="S")

    @field_validator("id", "fantasia", "razao", "cpf_cnpj", "ativo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @property
    def display_name(self) -> str:
        """Retorna fantasia se disponível, senão razao social, senão Fornecedor #id."""
        return self.fantasia or self.razao or f"Fornecedor #{self.id}"


# =============================================================================
# Add-ons de contrato — endpoint /cliente_contrato_servicos no IXC
# =============================================================================
class IxcContractServiceSchema(BaseModel):
    """Schema de cliente_contrato_servicos (add-ons de contrato)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_contrato: str = Field(...)
    descricao: str = Field(default="")
    valor_total: str = Field(default="0")
    status: str = Field(default="I")  # I=incluso/ativo, CA=cancelado
    tipo: str = Field(default="S")  # S=serviço, I=item

    @field_validator("id", "id_contrato", "descricao", "status", "tipo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor_total", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, ""):
            return "0"
        return str(v).replace(",", ".")


# =============================================================================
# Descontos de contrato — endpoint /cliente_contrato_descontos no IXC
# =============================================================================
class IxcContractDiscountSchema(BaseModel):
    """Schema de cliente_contrato_descontos."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_contrato: str = Field(...)
    descricao: str = Field(default="")
    valor: str = Field(default="0")
    percentual: str = Field(default="0")
    data_validade: str = Field(default="")

    @field_validator("id", "id_contrato", "descricao", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", "percentual", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, ""):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_validade", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> str:
        if v in (None, "", "0000-00-00"):
            return ""
        return str(v)[:10]


# =============================================================================
# Acréscimos de contrato — endpoint /cliente_contrato_acrescimos no IXC
# =============================================================================
class IxcContractSurchargeSchema(BaseModel):
    """Schema de cliente_contrato_acrescimos."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_contrato: str = Field(...)
    descricao: str = Field(default="")
    valor: str = Field(default="0")
    data_validade: str = Field(default="")

    @field_validator("id", "id_contrato", "descricao", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, ""):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_validade", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> str:
        if v in (None, "", "0000-00-00"):
            return ""
        return str(v)[:10]


# =============================================================================
# Chamado — endpoint /su_oss_chamado no IXC
# =============================================================================
class IxcTicketSchema(BaseModel):
    """Schema do registro `su_oss_chamado` (chamados de suporte) na API IXC.

    Status: AG=agendado, A=aberto, EX=em execucao, F=fechado, EN=encaminhado.
    Prioridade: N=normal, A=alta, B=baixa, U=urgente.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_cliente: str = Field(default="")
    id_assunto: str = Field(default="")
    setor: str = Field(default="")
    id_tecnico: str = Field(default="")
    status: str = Field(default="A")
    prioridade: str = Field(default="N")
    mensagem: str = Field(default="")
    protocolo: str = Field(default="")
    data_abertura: datetime | None = Field(default=None)
    data_agenda: datetime | None = Field(default=None)
    data_fechamento: datetime | None = Field(default=None)
    ultima_atualizacao: datetime | None = Field(default=None)

    @field_validator("id", "id_cliente", "id_assunto", "setor", "id_tecnico", "status", "prioridade", "protocolo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("mensagem", mode="before")
    @classmethod
    def _empty_to_str(cls, v: Any) -> str:
        if v in (None, "null"):
            return ""
        return str(v)

    @field_validator("data_abertura", "data_agenda", "data_fechamento", "ultima_atualizacao", mode="before")
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
