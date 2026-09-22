"""DTOs do domínio Network — neutros, sem campos source-specific."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class ConnectionDTO:
    """Representação neutra do estado de conexão de um cliente (RADIUS/PPPoE).

    `external_id` é opaco — string que identifica a conexão no sistema de origem.
    Combinado com `source_type` (que vive no adapter), forma a chave composta de
    persistência: `(organization, source_type, external_id)`.

    `status` deriva de ativo/online no adapter:
    ONLINE (ativo+online), OFFLINE (ativo+offline), BLOCKED (inativo), UNKNOWN.
    """

    external_id: str
    customer_external_id: str
    contract_external_id: str
    login: str
    status: str  # ONLINE, OFFLINE, BLOCKED, UNKNOWN

    ip: str = ""
    nas_ip: str = ""
    rx_bytes: int = 0
    tx_bytes: int = 0
    download_speed: str = ""
    upload_speed: str = ""

    last_connection_at: datetime | None = None

    # Topologia e queda — promovidos de `raw_extras` em #143. Deixaram de ser
    # detalhe de fonte no dia em que o detector de massivas passou a agrupar
    # queda por CTO/OLT e por proximidade: viraram campo essencial do domínio.
    cto_external_id: str = ""
    cto_port: str = ""
    # A PON vem do registro da ONU (radpop_radio_cliente_fibra), não da caixa —
    # ver IxcOnuFibraSchema: PON é propriedade do login, não da CTO.
    pon_external_id: str = ""
    # Id do registro da ONU na origem — a mesma leitura que traz a PON já o
    # entrega de graça. Não é enfeite: é a chave que o disparo de medição de
    # potência exige (#148), e sem ela medir um cliente custaria uma listagem
    # inteira só pra descobrir qual ONU é a dele.
    onu_external_id: str = ""
    transmitter_external_id: str = ""
    concentrator_external_id: str = ""
    latitude: float | None = None
    longitude: float | None = None
    disconnect_reason: str = ""
    last_disconnection_at: datetime | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("ConnectionDTO.external_id não pode ser vazio")


@dataclass(frozen=True)
class OpticalSignalDTO:
    """Leitura óptica de uma ONU — potência, causa da queda e estado na OLT (#148).

    Neutro de propósito: o domínio quer "qual era o sinal deste login e o que a
    OLT disse", não o nome dos campos do IXC.

    Duas ausências que o adapter é obrigado a traduzir antes de chegar aqui,
    porque confundi-las com valor é o que transforma a tela em gerador de alarme
    falso (medido em produção 2026-09-08):

    - **`signal_rx = None` é ausência de leitura, não 0 dBm.** O IXC devolve
      `0.00` em 1.391 dos 4.554 registros de ONU, e ~30% das ONUs simplesmente
      não reportam sinal. Zero dBm seria um sinal absurdamente forte; comparado
      com uma base de -24 dB, produziria "24 dB de perda" para todo cliente que
      caiu;
    - **`measured_at = None` é "nunca medido".** O IXC carrega o zero-date do
      MySQL (`0000-00-00 00:00:00`) como sentinela.

    `last_drop_cause` vazio é **"a OLT não informou"**, não "sem causa" — e o
    domínio não interpreta o texto. `dying-gasp` sugere que a ONU perdeu
    energia, mas essa leitura é do time, não do código: aqui se guarda o que a
    OLT disse, literalmente.
    """

    onu_external_id: str
    login_external_id: str = ""

    signal_rx: float | None = None
    signal_tx: float | None = None
    measured_at: datetime | None = None
    temperature: float | None = None
    voltage: float | None = None

    run_state: str = ""
    last_drop_cause: str = ""
    last_up_at: datetime | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.onu_external_id:
            raise ValueError("OpticalSignalDTO.onu_external_id não pode ser vazio")

    @property
    def has_signal(self) -> bool:
        """Leitura de potência utilizável — o que sustenta comparação antes/depois."""
        return self.signal_rx is not None and self.measured_at is not None


@dataclass(frozen=True)
class BandwidthUsageDTO:
    """Consumo de banda por cliente/período (accounting RADIUS).

    Cada registro representa o tráfego acumulado de um cliente em um período
    (tipicamente um dia). `download_bytes`/`upload_bytes` vêm dos contadores de
    accounting; `session_time` é o tempo conectado em segundos.

    `external_id` é opaco — identifica o registro de consumo na origem. Combinado
    com `source_type` forma a chave composta `(organization, source_type,
    external_id)`.
    """

    external_id: str
    customer_external_id: str

    download_bytes: int = 0
    upload_bytes: int = 0
    session_time: int = 0  # segundos

    reference_date: date | None = None

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("BandwidthUsageDTO.external_id não pode ser vazio")


# Tipos de elemento de rede reconhecidos pelo domínio. Tupla de strings (e não
# Enum) porque o DTO é neutro e atravessa a fronteira do adapter; quem restringe
# o vocabulário no banco é o model.
NETWORK_ELEMENT_KINDS: tuple[str, ...] = (
    "CTO", "POP", "PON", "OLT", "CABLE", "SPLICE", "SPLITTER", "POLE",
)


@dataclass(frozen=True)
class ElementGeometryDTO:
    """O traçado de um elemento da planta — a polilinha do projeto.

    Separado de `NetworkElementDTO` porque responde outra pergunta: o elemento
    diz *o que é e de quem depende*, a geometria diz *por onde passa*. Cabo tem
    traçado e não tem posição; CTO tem posição e não tem traçado.

    `points` é `((lat, lon), ...)` **na ordem do traçado**. A ordem é o dado — na
    origem ela vem no campo `sequencia`, e embaralhá-la transforma o cabo num
    zigue-zague que atravessa a cidade.

    Isto é cadastro, não medição: diz por onde o projeto passa o cabo, não onde a
    fibra está hoje nem onde ela rompeu.
    """

    external_id: str
    kind: str  # ver NETWORK_ELEMENT_KINDS
    points: tuple[tuple[float, float], ...]

    name: str = ""
    project_external_id: str = ""
    # O nome do TIPO na origem ("CLIENTE DROP 1FO", "FIBRA AS80 12FO BACKBONE").
    # É ele que diz a classe do cabo — o nome do próprio elemento só diz em 47%
    # dos casos, e 57 cabos de tipo drop se chamam apenas "01FO".
    type_name: str = ""
    # Id da coordenada na origem, um por vértice e na ordem de `points`. É a
    # ligação real da planta: elementos que se conectam compartilham o mesmo id
    # (medido: 1.814 coordenadas com 2+ elementos). Vazio quando a origem não
    # expõe — e aí quem consome cai na inferência por distância.
    coordinate_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("ElementGeometryDTO.external_id não pode ser vazio")
        if self.kind not in NETWORK_ELEMENT_KINDS:
            raise ValueError(
                f"ElementGeometryDTO.kind inválido: {self.kind!r} "
                f"(esperado um de {NETWORK_ELEMENT_KINDS})"
            )
        if not self.points:
            raise ValueError(
                "ElementGeometryDTO.points vazio — elemento sem ponto não tem "
                "traçado, e gravar a lista vazia faria a tela desenhar nada "
                "achando que desenhou algo"
            )

    @property
    def is_line(self) -> bool:
        """Dois pontos são o mínimo para haver traçado; um ponto é posição."""
        return len(self.points) >= 2


@dataclass(frozen=True)
class NetworkElementDTO:
    """Elemento da planta de rede — caixa FTTH, POP, porta PON, OLT ou cabo.

    O que importa pro domínio é `kind` mais a ligação com o pai
    (`parent_kind`/`parent_external_id`): é ela que permite subir a hierarquia
    POP → OLT → PON → CTO quando várias quedas coincidem no tempo.

    `latitude`/`longitude` podem faltar — o cabo não tem *posição*, tem traçado,
    e o traçado mora em `ElementGeometryDTO`. Por isso são `None` e não 0.0: um
    par (0, 0) cairia no golfo da Guiné e entraria em cluster geográfico com
    tudo.

    Identidade composta na persistência: `(organization, source_type, kind,
    external_id)`. `kind` entra na chave porque os ids são sequências por tabela
    na origem — a CTO 12 não é o POP 12.
    """

    external_id: str
    kind: str  # ver NETWORK_ELEMENT_KINDS

    name: str = ""
    latitude: float | None = None
    longitude: float | None = None

    parent_external_id: str = ""
    parent_kind: str = ""

    capacity: int | None = None
    address: str = ""
    project_external_id: str = ""
    status: str = ""

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("NetworkElementDTO.external_id não pode ser vazio")
        if self.kind not in NETWORK_ELEMENT_KINDS:
            raise ValueError(
                f"NetworkElementDTO.kind inválido: {self.kind!r} "
                f"(esperado um de {NETWORK_ELEMENT_KINDS})"
            )

    @property
    def has_position(self) -> bool:
        return self.latitude is not None and self.longitude is not None
