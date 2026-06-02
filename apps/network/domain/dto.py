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

    raw_extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.external_id:
            raise ValueError("ConnectionDTO.external_id não pode ser vazio")


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
