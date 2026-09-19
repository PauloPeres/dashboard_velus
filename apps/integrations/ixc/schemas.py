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

from datetime import date, datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


def _to_str(v: Any) -> str:
    """IXC ora retorna int, ora string. Normaliza pra string."""
    return str(v) if v is not None else ""


def _to_id_str(v: Any) -> str:
    """Normaliza id de referência do IXC — "0" e "" significam a mesma coisa: sem vínculo.

    O IXC usa "0" como "não relacionado" em toda FK. Deixar o zero passar faria
    o domínio tratar "sem CTO" como "CTO de id 0" e agrupar clientes de bairros
    diferentes na mesma caixa fantasma.
    """
    raw = _to_str(v).strip()
    return "" if raw in ("0", "0.0") else raw


def _to_coordinate(v: Any) -> float | None:
    """Coordenada do IXC (string) → float, com 0 virando None.

    Zero não é uma posição: é o campo em branco. Se passasse como 0.0 o ponto
    cairia no golfo da Guiné e entraria em qualquer cluster geográfico.
    """
    if v in (None, ""):
        return None
    try:
        coord = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return None if coord == 0.0 else coord


def _to_ixc_datetime(v: Any) -> datetime | None:
    """Datas do IXC vêm sem timezone e com o zero-date do MySQL como "vazio"."""
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


# =============================================================================
# Conexão RADIUS — endpoint /radusuarios no IXC
# =============================================================================
class IxcRadUserSchema(BaseModel):
    """Schema do registro `radusuarios` (RADIUS/PPPoE) na API IXC.

    `ativo` e `online` são flags S/N. `download`/`upload` são velocidades do
    plano (strings opacas). `tempo_conectado_*` e bytes refletem a sessão atual.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_cliente: str = Field(default="")
    id_contrato: str = Field(default="")
    login: str = Field(default="")
    ativo: str = Field(default="N")
    online: str = Field(default="N")
    ip: str = Field(default="")
    nas_ip: str = Field(default="")
    download: str = Field(default="")
    upload: str = Field(default="")
    bytes_recebidos: int = Field(default=0)
    bytes_enviados: int = Field(default=0)
    # O IXC renomeou `ultima_conexao` p/ `ultima_conexao_inicial` (início da
    # última sessão); aceita os dois p/ não perder o last_connection_at.
    ultima_conexao: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("ultima_conexao", "ultima_conexao_inicial"),
    )

    # Topologia e queda — promovidos a coluna em #143 (antes só chegavam em
    # `raw_extras` e ninguém lia). São a base do detector de massivas.
    ultima_conexao_final: datetime | None = Field(default=None)
    motivo_desconexao: str = Field(default="")
    id_caixa_ftth: str = Field(default="")
    ftth_porta: str = Field(default="")
    id_transmissor: str = Field(default="")
    id_concentrador: str = Field(default="")
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)

    @field_validator(
        "id", "id_cliente", "id_contrato", "login", "ativo", "online",
        "ip", "nas_ip", "download", "upload", "motivo_desconexao", "ftth_porta",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator(
        "id_caixa_ftth", "id_transmissor", "id_concentrador", mode="before"
    )
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _coerce_coordinate(cls, v: Any) -> float | None:
        return _to_coordinate(v)

    @field_validator("ultima_conexao_final", mode="before")
    @classmethod
    def _parse_disconnection(cls, v: Any) -> datetime | None:
        return _to_ixc_datetime(v)

    @field_validator("bytes_recebidos", "bytes_enviados", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        if v in (None, ""):
            return 0
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    @field_validator("ultima_conexao", mode="before")
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

    @property
    def is_active(self) -> bool:
        return self.ativo.upper() == "S"

    @property
    def is_online(self) -> bool:
        return self.online.upper() == "S"

    @property
    def has_known_session_state(self) -> bool:
        """`online` só distingue conectado de caído nos valores "S" e "N".

        Medição em produção (2026-09-08, 8.265 logins): "S" 3.129, "SS" 4.870,
        "N" 245, "" 21. Nos 3.413 logins ATIVOS o campo é "S" 3.125, "N" 236,
        "SS" 31, "" 21.

        "SS" NÃO é sessão simultânea: dos 31 ativos com esse valor, nenhum tem
        IP, 28 nunca conectaram (`ultima_conexao_inicial` zerada) e os 3 que
        conectaram pararam em 2025 — é o estado de login sem sessão registrada,
        que é também o que 4.839 dos 4.848 logins inativos carregam. Os 21 com
        campo vazio têm última conexão em 2021.

        Nenhum dos dois é uma queda de hoje. Tratá-los como OFFLINE colocaria 52
        clientes fantasma competindo com as ~44 quedas do maior evento real do
        dia e afundaria a credibilidade do detector — por isso viram UNKNOWN.
        Não colapse isto de volta em `online != "S"`.
        """
        return self.online.upper() in ("S", "N")

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class IxcBandwidthSchema(BaseModel):
    """Schema do registro `radusuarios_consumo` (accounting RADIUS) na API IXC.

    Cada registro é o consumo acumulado de um cliente num período (tipicamente
    um dia). `acctinputoctets`/`acctoutputoctets` são os contadores de bytes;
    `acctsessiontime` é o tempo conectado em segundos. `data` é a data de
    referência do consumo.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_cliente: str = Field(default="")
    acctinputoctets: int = Field(default=0)  # download
    acctoutputoctets: int = Field(default=0)  # upload
    acctsessiontime: int = Field(default=0)  # segundos
    data: date | None = Field(default=None)

    @field_validator("id", "id_cliente", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator(
        "acctinputoctets", "acctoutputoctets", "acctsessiontime", mode="before"
    )
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        if v in (None, ""):
            return 0
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    @field_validator("data", mode="before")
    @classmethod
    def _parse_ixc_date(cls, v: Any) -> date | None:
        if v in (None, "", "0000-00-00 00:00:00", "0000-00-00"):
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(v, fmt).date()
                except ValueError:
                    continue
        return None

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class IxcPaymentSchema(BaseModel):
    """Schema do registro `fn_areceber_baixas` (baixas de recebíveis) na API IXC.

    Cada baixa é um recebimento efetivo de uma fatura (`id_areceber`). Suporta
    pagamentos parciais e múltiplas baixas por fatura. `juros`, `multa` e
    `desconto` detalham a composição do valor recebido — preservados em
    raw_extras pra análise de recuperação de inadimplência.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    # NOTA: o endpoint real entrega os campos com nomes diferentes dos canônicos
    # (id_receber/data/valor_liquido_recebido). Usamos AliasChoices pra aceitar
    # ambos — sem isso, data_baixa ficava None e _to_dto descartava 100% das
    # baixas, deixando Payment vazio (#26).
    id: str = Field(...)
    id_areceber: str = Field(
        default="",
        validation_alias=AliasChoices("id_areceber", "id_receber"),
    )  # FK pra fatura (fn_areceber)
    id_cliente: str = Field(default="")
    valor: str = Field(
        default="0",
        validation_alias=AliasChoices("valor", "valor_liquido_recebido", "credito"),
    )  # valor recebido na baixa
    data_baixa: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("data_baixa", "data"),
    )
    # tipo_recebimento é um código contábil (débito/crédito), não o meio de
    # pagamento — o método real só é legível no histórico (texto livre).
    forma_pagamento: str = Field(
        default="",
        validation_alias=AliasChoices("forma_pagamento", "tipo_recebimento"),
    )
    historico: str = Field(default="")
    juros: str = Field(default="0")
    multa: str = Field(default="0")
    desconto: str = Field(default="0")

    @field_validator(
        "id", "id_areceber", "id_cliente", "forma_pagamento", "historico", mode="before"
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", "juros", "multa", "desconto", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, "", "0.00"):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_baixa", mode="before")
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
# Equipamento em comodato — endpoint /cliente_contrato_comodato no IXC
# =============================================================================
class IxcEquipmentSchema(BaseModel):
    """Schema do registro `cliente_contrato_comodato` na API IXC.

    Equipamentos (ONT, roteador, switch) emprestados ao cliente, atrelados a um
    contrato (`id_cliente_contrato`). `status` na origem costuma ser A=Ativo
    (em campo) / D=Devolvido — mapeado pro status canônico no adapter.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    # A API real expõe `id_contrato`; mantemos o alias canônico para fixtures.
    id_cliente_contrato: str = Field(
        default="",
        validation_alias=AliasChoices("id_cliente_contrato", "id_contrato"),
    )  # FK pro contrato
    id_produto: str = Field(default="")
    descricao: str = Field(default="")  # nome do produto, quando a API resolve
    # API real: `numero_serie`.
    serial: str = Field(
        default="",
        validation_alias=AliasChoices("serial", "numero_serie"),
    )
    mac: str = Field(default="")
    # API real: `valor_total` (cai pra `valor_unitario` quando ausente).
    valor: str = Field(
        default="0",
        validation_alias=AliasChoices("valor", "valor_total", "valor_unitario"),
    )
    # API real: `status_comodato` — E=Entregue/em campo, D=Devolvido, B=Baixado.
    status: str = Field(
        default="",
        validation_alias=AliasChoices("status", "status_comodato"),
    )

    @field_validator(
        "id", "id_cliente_contrato", "id_produto", "descricao",
        "serial", "mac", "status", mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, "", "0.00"):
            return "0"
        return str(v).replace(",", ".")

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


# =============================================================================
# CRM — leads (crm_canditados) e negociações (crm_negociacoes)
# =============================================================================
class IxcLeadSchema(BaseModel):
    """Schema do registro `crm_canditados` na API IXC (leads/prospects).

    Campos centrais do CRM IXC. `status_prospeccao` é o estágio do lead no
    funil (mapeado pro status canônico no adapter). `origem` quando presente
    indica o canal (indicação, site, redes sociais).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    nome: str = Field(default="")
    telefone: str = Field(default="")
    email: str = Field(default="")
    status_prospeccao: str = Field(default="")
    origem: str = Field(default="")
    id_vendedor: str = Field(default="")
    data_cadastro: datetime | None = Field(default=None)

    @field_validator(
        "id", "nome", "telefone", "email", "status_prospeccao",
        "origem", "id_vendedor", mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("data_cadastro", mode="before")
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


class IxcOpportunitySchema(BaseModel):
    """Schema do registro `crm_negociacoes` na API IXC (negociações).

    `id_candidato` é a FK pro lead. `status` indica o estado da negociação
    (mapeado pro status canônico no adapter). `valor` vem string formato
    "150.00" ou "150,00".
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    id: str = Field(...)
    id_candidato: str = Field(default="")
    valor: str = Field(default="0")
    status: str = Field(default="")
    motivo_perda: str = Field(default="")
    data_criacao: datetime | None = Field(default=None)
    data_fechamento: datetime | None = Field(default=None)

    @field_validator(
        "id", "id_candidato", "status", "motivo_perda", mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("valor", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> str:
        if v in (None, "", "0.00"):
            return "0"
        return str(v).replace(",", ".")

    @field_validator("data_criacao", "data_fechamento", mode="before")
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
# Topologia de rede (#142) — planta física: POP, OLT, porta PON, CTO e cabo
# =============================================================================
class IxcCaixaFtthSchema(BaseModel):
    """Schema do registro `rad_caixa_ftth` — a CTO (caixa de atendimento FTTH).

    1.445 linhas em produção. `id_transmissor` está preenchido em todas elas
    (1021/272/152 entre as 3 OLTs), então o degrau de OLT pode confiar nele.
    """

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    descricao: str = Field(default="")
    capacidade: int = Field(default=0)
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)
    id_transmissor: str = Field(default="")
    # Aponta pra porta PON, mas só em 420 das 1.445 caixas — ver comentário em
    # IxcOnuFibraSchema sobre por que a PON não vem daqui.
    id_interface: str = Field(default="")
    id_projeto: str = Field(default="")
    status: str = Field(default="")
    tipo: str = Field(default="")
    cep: str = Field(default="")
    endereco: str = Field(default="")
    numero: str = Field(default="")
    bairro: str = Field(default="")
    id_cidade: str = Field(default="")
    obs_caixa_ftth: str = Field(default="")

    @field_validator(
        "id", "descricao", "id_projeto", "status", "tipo", "cep",
        "endereco", "numero", "bairro", "id_cidade", "obs_caixa_ftth",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("id_transmissor", "id_interface", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)

    @field_validator("capacidade", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        if v in (None, ""):
            return 0
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _coerce_coordinate(cls, v: Any) -> float | None:
        return _to_coordinate(v)

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class IxcRadPopSchema(BaseModel):
    """Schema do registro `radpop` — o POP (ponto de presença). 9 linhas."""

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    pop: str = Field(default="")
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)
    id_projeto: str = Field(default="")
    id_cidade: str = Field(default="")
    endereco: str = Field(default="")
    numero: str = Field(default="")
    bairro: str = Field(default="")
    tp_estacao: str = Field(default="")

    @field_validator(
        "id", "pop", "id_projeto", "id_cidade", "endereco", "numero",
        "bairro", "tp_estacao",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _coerce_coordinate(cls, v: Any) -> float | None:
        return _to_coordinate(v)

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class IxcRadPopRadioSchema(BaseModel):
    """Schema do registro `radpop_radio` — a OLT. 3 linhas em produção.

    `extra="ignore"` (e não "allow", como o resto do arquivo) porque este
    registro carrega as senhas de gerência do equipamento (`senha`, `senha_hw`,
    `senha_anm`). Guardá-las em `raw_extras` colocaria credencial de acesso à
    OLT dentro do banco do dashboard — que é read-only e não tem por que sabê-las.
    """

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    descricao: str = Field(default="")
    id_pop: str = Field(default="")
    ativo: str = Field(default="")
    modelo: str = Field(default="")
    fabricante_modelo: str = Field(default="")

    @field_validator(
        "id", "descricao", "ativo", "modelo", "fabricante_modelo", mode="before"
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("id_pop", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)


class IxcPortaPonSchema(BaseModel):
    """Schema do registro `radpop_radio_porta_fibra` — a porta PON. 307 linhas.

    `id_pop_radio` é a OLT (bate com `radpop_radio.id` e com o `id_transmissor`
    das caixas e das ONUs: 1, 2 e 3).
    """

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    id_pop_radio: str = Field(default="")
    id_slot: str = Field(default="")
    numero_pon: str = Field(default="")
    interface: str = Field(default="")
    potencia_pon: str = Field(default="")
    potencia_limite: str = Field(default="")
    quantidade_onus: int = Field(default=0)

    @field_validator(
        "id", "id_slot", "numero_pon", "interface", "potencia_pon",
        "potencia_limite",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("id_pop_radio", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)

    @field_validator("quantidade_onus", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        if v in (None, ""):
            return 0
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class IxcDfElementoSchema(BaseModel):
    """Schema do registro `df_elemento` filtrado por `tipo=CB` — o cabo. 1.191 linhas.

    **Tem geometria**, ao contrário do que este docstring afirmou até
    2026-09-19: o traçado sai de `df_elemento_coordenada` + `df_coordenada`
    (`IxcDfElementoCoordenadaSchema` e `IxcDfCoordenadaSchema`, abaixo). O que
    esta linha não tem é *posição* — cabo é linha, não ponto.
    """

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    descricao: str = Field(default="")
    id_tipo_elemento: str = Field(default="")
    id_projeto: str = Field(default="")
    tipo: str = Field(default="")
    observacao: str = Field(default="")
    ultima_atualizacao: datetime | None = Field(default=None)

    @field_validator(
        "id", "descricao", "id_tipo_elemento", "id_projeto", "tipo",
        "observacao",
        mode="before",
    )
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("ultima_atualizacao", mode="before")
    @classmethod
    def _parse_dt(cls, v: Any) -> datetime | None:
        return _to_ixc_datetime(v)

    def get_extras(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class IxcDfElementoCoordenadaSchema(BaseModel):
    """Vínculo elemento → coordenada, do InMap. 12.922 linhas.

    O campo que importa é `sequencia`: é a **ordem do vértice** no traçado.
    Ordenar por `id` daria a ordem de gravação, que não é a do cabo.

    Quirk de API: o filtro é `df_elemento_coordenada.id_elemento`. Pedir por
    `id_df_elemento` devolve a página HTML de erro do IXC.
    """

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    id_elemento: str = Field(default="")
    id_coordenada: str = Field(default="")
    sequencia: int = Field(default=0)

    @field_validator("id", "id_elemento", "id_coordenada", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("sequencia", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        texto = _to_str(v)
        try:
            return int(texto)
        except ValueError:
            # Sequência ilegível vira 0 e o vértice vai para o começo. Descartar
            # a linha seria pior: some um pedaço do cabo sem ninguém ver.
            return 0


class IxcDfCoordenadaSchema(BaseModel):
    """O ponto — latitude e longitude. 10.520 linhas.

    A existência deste endpoint é o achado do spike R2: o plano afirmava, sem
    ter testado, que a tabela de coordenadas não era acessível pela API.
    """

    model_config = ConfigDict(
        extra="allow", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _coerce_float(cls, v: Any) -> float | None:
        texto = _to_str(v)
        if not texto:
            return None
        try:
            return float(texto)
        except ValueError:
            return None

    @property
    def has_position(self) -> bool:
        # (0, 0) é ausência disfarçada: cairia no golfo da Guiné.
        return (
            self.latitude is not None
            and self.longitude is not None
            and (self.latitude, self.longitude) != (0.0, 0.0)
        )


class IxcOnuFibraSchema(BaseModel):
    """Schema do registro `radpop_radio_cliente_fibra` — a ONU do cliente.

    É daqui que sai a porta PON do login, e não de `rad_caixa_ftth.id_interface`.
    Medido em produção (2026-09-08):

    - `rad_caixa_ftth.id_interface`: preenchido em 420 das 1.445 caixas (29%);
    - `radpop_radio_cliente_fibra.id_radpop_radio_porta`: 4.553 de 4.559 (99,9%).

    E o motivo estrutural, que é o que impede a "simplificação": derivando
    CTO → PON pelos clientes, 239 das 927 caixas deriváveis (26%) apontam pra
    mais de uma porta. **PON é propriedade do login, não da caixa** — uma mesma
    CTO pode ser alimentada por mais de uma PON. Qualquer mapa CTO → PON estaria
    errado em um quarto dos casos.

    O join `id_login` → `radusuarios.id` foi verificado: casa em 4.081 de 4.081
    linhas com login e porta preenchidos, cobrindo 3.323 dos 3.413 logins ativos
    (97,4%) e 3.096 dos 3.125 online (99,1%). Nenhum login aponta pra 2 PONs.
    """

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    id_login: str = Field(default="")
    id_radpop_radio_porta: str = Field(default="")
    id_caixa_ftth: str = Field(default="")
    porta_ftth: str = Field(default="")
    id_transmissor: str = Field(default="")
    id_contrato: str = Field(default="")
    onu_tipo: str = Field(default="")
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)

    @field_validator("id", "porta_ftth", "onu_tipo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator(
        "id_login", "id_radpop_radio_porta", "id_caixa_ftth", "id_transmissor",
        "id_contrato",
        mode="before",
    )
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _coerce_coordinate(cls, v: Any) -> float | None:
        return _to_coordinate(v)


def _to_optical_reading(v: Any) -> float | None:
    """Leitura óptica do IXC (string) → float, com **zero virando None**.

    Esta é a armadilha central do sinal óptico (#148), medida em produção
    (2026-09-08): `sinal_rx = "0.00"` aparece em 1.391 dos 4.554 registros de
    ONU e significa **ausência de leitura**, não 0 dBm. Zero dBm seria uma
    potência absurdamente alta; comparado com a linha de base típica (-24 dB),
    faria todo cliente que caiu aparecer com "24 dB de perda" e transformaria a
    tela em gerador de alarme falso.

    Vale igual para temperatura e voltagem: as quatro grandezas vêm zeradas
    juntas quando a ONU não reportou.
    """
    if v in (None, ""):
        return None
    try:
        value = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return None if value == 0.0 else value


def _to_reported_text(v: Any) -> str:
    """Texto que a OLT reporta, com os sentinelas de "não informou" virando vazio.

    O IXC usa `"-"` para "a OLT não devolveu este campo" — visto tanto em
    `causa_ultima_queda` (43 de 300 ONUs amostradas) quanto no painel do botão
    de potência (`Last dying gasp time: -`). Deixá-lo passar faria a UI exibir
    um traço como se fosse a causa da queda.

    O conteúdo em si **não é interpretado**: `dying-gasp`, `LOS`, `LOSi/LOBi`,
    `reset` e o que mais a OLT inventar entram literais. Traduzir isso é leitura
    do time, não do código.
    """
    raw = _to_str(v).strip()
    return "" if raw in ("-", "--") else raw


class IxcOnuSignalSchema(BaseModel):
    """Schema de `radpop_radio_cliente_fibra` na leitura de **sinal óptico** (#148).

    Separado de `IxcOnuFibraSchema` (que existe para derivar a PON do login) por
    duas razões: são leituras com cadência e propósito diferentes, e aqui as
    coerções são outras — zero é ausência, não valor.

    `extra="ignore"` com `raw_extras` montado à mão, e não `extra="allow"`: o
    registro da ONU carrega credenciais de gerência do equipamento
    (`senha_onu_cliente`, `porta_telnet_onu_cliente`, `script_onu_cliente`, e os
    templates de comando com senha de roteador do cliente). Nenhuma delas pode
    vazar para o banco do dashboard — mesmo motivo do schema da OLT (§10.3 do
    plano de massivas).

    Achado de produção (2026-09-08) que o plano não previa: **`causa_ultima_queda`
    vem na própria listagem**. Ou seja, a causa da queda não depende do disparo
    ativo nem de scraping — o caminho barato já a traz. Valores observados em 300
    ONUs: vazio (213), `-` (43), `dying-gasp` (28), `LOSi/LOBi` (9), `reset` (3),
    `ONT` (3), `LOS` (1).
    """

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    id_login: str = Field(default="")

    sinal_rx: float | None = Field(default=None)
    sinal_tx: float | None = Field(default=None)
    temperatura: float | None = Field(default=None)
    voltagem: float | None = Field(default=None)
    data_sinal: datetime | None = Field(default=None)

    causa_ultima_queda: str = Field(default="")

    # Contexto de localização física — útil pro técnico e barato de carregar.
    ponid: str = Field(default="")
    mac: str = Field(default="")
    onu_tipo: str = Field(default="")

    @field_validator("id", "ponid", "mac", "onu_tipo", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("id_login", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)

    @field_validator("causa_ultima_queda", mode="before")
    @classmethod
    def _coerce_cause(cls, v: Any) -> str:
        return _to_reported_text(v)

    @field_validator(
        "sinal_rx", "sinal_tx", "temperatura", "voltagem", mode="before"
    )
    @classmethod
    def _coerce_reading(cls, v: Any) -> float | None:
        return _to_optical_reading(v)

    @field_validator("data_sinal", mode="before")
    @classmethod
    def _parse_dt(cls, v: Any) -> datetime | None:
        return _to_ixc_datetime(v)

    def get_extras(self) -> dict[str, Any]:
        """Extras montados à mão — ver docstring sobre credenciais de gerência."""
        return {
            key: value
            for key, value in (
                ("ponid", self.ponid),
                ("mac", self.mac),
                ("onu_tipo", self.onu_tipo),
            )
            if value
        }


class IxcOnuSignalHistorySchema(BaseModel):
    """Schema de `radpop_radio_cliente_fibra_historico` — a série do sinal (#148).

    1,93M linhas em produção; a coleta é **diária** (~06:30), então esta série é
    linha de base e não leitura pós-reparo. Aceita filtro por
    `qtype=radpop_radio_cliente_fibra_historico.id_cliente_fibra` e já vem
    ordenada do mais recente para o mais antigo.

    Cobertura não é universal: a ONU 10733, que tem sinal corrente válido,
    devolve **zero linhas** de histórico. Ausência de série é normal e não é
    erro — por isso a busca da linha de base degrada em silêncio.
    """

    model_config = ConfigDict(
        extra="ignore", populate_by_name=True, str_strip_whitespace=True
    )

    id: str = Field(...)
    id_cliente_fibra: str = Field(default="")

    sinal_rx: float | None = Field(default=None)
    sinal_tx: float | None = Field(default=None)
    temperatura: float | None = Field(default=None)
    voltagem: float | None = Field(default=None)
    data_sinal: datetime | None = Field(default=None)

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return _to_str(v)

    @field_validator("id_cliente_fibra", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return _to_id_str(v)

    @field_validator(
        "sinal_rx", "sinal_tx", "temperatura", "voltagem", mode="before"
    )
    @classmethod
    def _coerce_reading(cls, v: Any) -> float | None:
        return _to_optical_reading(v)

    @field_validator("data_sinal", mode="before")
    @classmethod
    def _parse_dt(cls, v: Any) -> datetime | None:
        return _to_ixc_datetime(v)
