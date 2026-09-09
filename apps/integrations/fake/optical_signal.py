"""FakeOpticalSignalSource — adapter in-memory de sinal óptico, pra testes (#148).

Além de dispensar o IXC nos testes, este fake **conta as medições ativas**
(`measure_calls`). Isso não é conveniência: a regra que define a feature é
"nenhuma chamada à OLT sem retorno de cliente", e regra sem contador é promessa.
O teste que garante zero chamadas quando ninguém voltou lê exatamente daqui.

O seed distingue os três caminhos do port justamente porque a realidade os
distingue: a leitura corrente pode estar zerada (ausência), o histórico pode não
existir, e a medição ativa pode voltar vazia (a OLT não conhece mais a ONU).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import OpticalSignalDTO

_seed_current: dict[str, OpticalSignalDTO] = {}
_seed_history: dict[str, list[OpticalSignalDTO]] = {}
_seed_measurements: dict[str, OpticalSignalDTO | None] = {}
_measure_calls: list[str] = []


class FakeOpticalSignalSource:
    source_type = SourceType.FAKE
    capabilities = frozenset({Capability.OPTICAL_SIGNAL})

    def __init__(self, **_credentials: Any) -> None:
        pass

    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------

    @classmethod
    def set_seed(
        cls,
        *,
        current: list[OpticalSignalDTO] | None = None,
        history: dict[str, list[OpticalSignalDTO]] | None = None,
        measurements: dict[str, OpticalSignalDTO | None] | None = None,
    ) -> None:
        global _seed_current, _seed_history, _seed_measurements
        _seed_current = {dto.onu_external_id: dto for dto in (current or [])}
        _seed_history = {k: list(v) for k, v in (history or {}).items()}
        _seed_measurements = dict(measurements or {})

    @classmethod
    def reset_seed(cls) -> None:
        global _seed_current, _seed_history, _seed_measurements, _measure_calls
        _seed_current = {}
        _seed_history = {}
        _seed_measurements = {}
        _measure_calls = []

    @classmethod
    def measure_calls(cls) -> list[str]:
        """ONUs medidas ativamente desde o último reset, na ordem em que foram."""
        return list(_measure_calls)

    # ------------------------------------------------------------------
    # Port
    # ------------------------------------------------------------------

    def list_optical_signals(self) -> Iterator[OpticalSignalDTO]:
        yield from _seed_current.values()

    def get_optical_signal(self, *, onu_external_id: str) -> OpticalSignalDTO | None:
        return _seed_current.get(onu_external_id)

    def last_valid_signal_before(
        self, *, onu_external_id: str, before: datetime
    ) -> OpticalSignalDTO | None:
        """Espelha o IXC: pula leitura sem potência e leitura posterior à queda.

        O filtro por `has_signal` é o ponto — o seed pode (e deve) conter linhas
        zeradas, que é como o histórico real vem.
        """
        candidatas = [
            dto
            for dto in _seed_history.get(onu_external_id, [])
            if dto.has_signal and dto.measured_at is not None and dto.measured_at < before
        ]
        if not candidatas:
            return None
        return max(candidatas, key=lambda dto: dto.measured_at)

    def measure_now(self, *, onu_external_id: str) -> OpticalSignalDTO | None:
        _measure_calls.append(onu_external_id)
        # Ausência do seed = a OLT não conhece essa ONU. É o caso majoritário
        # medido em produção: de 15 ONUs de logins offline, 10 não devolveram
        # campo nenhum.
        return _seed_measurements.get(onu_external_id)
