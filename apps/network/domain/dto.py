"""DTOs do domínio Network — neutros, sem campos source-specific."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
