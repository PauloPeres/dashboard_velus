"""IxcOpticalSignalSource — sinal óptico e causa de queda da ONU (#148).

Três caminhos, dois baratos e um caro, e a diferença entre eles é a decisão de
arquitetura inteira desta feature (`docs/massivas-plano.md` §2.8):

1. `radpop_radio_cliente_fibra` (listagem) — leitura corrente de todas as ONUs
   numa chamada. Alimentada pela varredura **diária** do IXC (~06:30): 4.551 das
   4.554 leituras têm de 6 a 24 horas. É **linha de base**, não leitura
   pós-reparo. Traz de brinde `causa_ultima_queda`, que o plano supunha só
   existir no painel do botão;
2. `radpop_radio_cliente_fibra_historico` (listagem filtrada) — a série, 1,93M
   linhas. Serve para achar a **última leitura válida** antes de uma queda quando
   a leitura corrente já está zerada ou é posterior à queda;
3. `POST botao_rel_22991` — **medição ativa**: consulta a OLT ao vivo, ~1,7 s
   (4,1 s no pior caso), funciona com a ONU fora do ar. É a única escrita
   autorizada do projeto (§2.9) e não grava dado de negócio: pede uma medição.

Anti-Corruption Layer: nada aqui levanta para fora. Falha de rede, JSON
inválido, schema mudado, painel HTML irreconhecível — tudo degrada para "sem
leitura" (`None` / iterador vazio). O sinal é enriquecimento; o dado principal da
aba é o estado do login, e ele não pode cair junto.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from typing import Any, ClassVar

import structlog
from pydantic import ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import OpticalSignalDTO

from .client import IxcHttpClient
from .onu_report import onu_report_fields
from .schemas import IxcOnuSignalHistorySchema, IxcOnuSignalSchema

_logger = structlog.get_logger(__name__)

# Endpoint "Potência/Resumo ONU" do IXC. O número é o id do botão no ERP — não é
# adivinhável, veio da collection Postman oficial e foi confirmado em produção.
MEASURE_RESOURCE = "botao_rel_22991"

_LISTING_RESOURCE = "radpop_radio_cliente_fibra"
_HISTORY_RESOURCE = "radpop_radio_cliente_fibra_historico"

# Quantas linhas de histórico varrer atrás da última leitura válida. A coleta é
# diária, então 30 linhas cobrem cerca de um mês — e uma ONU que não reporta há
# um mês não tem linha de base a oferecer de qualquer forma.
_HISTORY_MAX_ROWS = 30


class IxcOpticalSignalSource:
    """Adapter IXC para a capability OPTICAL_SIGNAL."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.OPTICAL_SIGNAL}
    )

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    # ------------------------------------------------------------------
    # Leitura passiva — barata, diária, não toca na OLT
    # ------------------------------------------------------------------

    def list_optical_signals(self) -> Iterator[OpticalSignalDTO]:
        """Leitura corrente de todas as ONUs (4.559 linhas hoje, uma listagem)."""
        body_filter = {
            "qtype": f"{_LISTING_RESOURCE}.id",
            "query": "1",
            "oper": ">=",
        }
        try:
            with self._client_factory() as client:
                for raw in client.paginate_ixc(
                    _LISTING_RESOURCE, body_filter=body_filter
                ):
                    dto = self._listing_to_dto(raw)
                    if dto is not None:
                        yield dto
        except Exception as exc:
            # Enriquecimento não derruba ninguém: o que já veio vale, o resto
            # espera a próxima rodada.
            _logger.warning(
                "ixc_optical_signal_list_failed",
                error=f"{type(exc).__name__}: {exc}"[:200],
            )

    def get_optical_signal(self, *, onu_external_id: str) -> OpticalSignalDTO | None:
        if not onu_external_id:
            return None
        body_filter = {
            "qtype": f"{_LISTING_RESOURCE}.id",
            "query": str(onu_external_id),
            "oper": "=",
        }
        try:
            with self._client_factory() as client:
                for raw in client.paginate_ixc(
                    _LISTING_RESOURCE, body_filter=body_filter
                ):
                    return self._listing_to_dto(raw)
        except Exception as exc:
            _logger.warning(
                "ixc_optical_signal_get_failed",
                onu=onu_external_id,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        return None

    def last_valid_signal_before(
        self, *, onu_external_id: str, before: datetime
    ) -> OpticalSignalDTO | None:
        """Última leitura com potência anterior a `before`, varrendo o histórico.

        "Com potência" é a razão de existir do método (armadilha 1 do #148):
        `sinal_rx = 0.00` aparece em 1.391 dos 4.554 registros e é **ausência de
        leitura**. Se a linha de base fosse simplesmente "a última linha", todo
        cliente que caiu apareceria com ~24 dB de perda e a tela viraria um
        gerador de alarme falso.

        Cobertura não é universal e isso é normal: a ONU 10733, com sinal
        corrente válido, tem zero linhas de histórico. Sem série, devolve `None`
        — ausência guardada como ausência.
        """
        if not onu_external_id:
            return None
        body_filter = {
            "qtype": f"{_HISTORY_RESOURCE}.id_cliente_fibra",
            "query": str(onu_external_id),
            "oper": "=",
            "sortname": f"{_HISTORY_RESOURCE}.data_sinal",
            "sortorder": "desc",
        }
        try:
            with self._client_factory() as client:
                rows = client.paginate_ixc(
                    _HISTORY_RESOURCE,
                    body_filter=body_filter,
                    page_size=_HISTORY_MAX_ROWS,
                )
                for index, raw in enumerate(rows):
                    if index >= _HISTORY_MAX_ROWS:
                        break
                    try:
                        row = IxcOnuSignalHistorySchema.model_validate(raw)
                    except ValidationError:
                        continue
                    if row.sinal_rx is None or row.data_sinal is None:
                        continue  # zero/zero-date = sem leitura, siga procurando
                    if row.data_sinal >= before:
                        continue  # posterior à queda: não é "antes"
                    return OpticalSignalDTO(
                        onu_external_id=onu_external_id,
                        signal_rx=row.sinal_rx,
                        signal_tx=row.sinal_tx,
                        measured_at=row.data_sinal,
                        temperature=row.temperatura,
                        voltage=row.voltagem,
                    )
        except Exception as exc:
            _logger.warning(
                "ixc_optical_signal_history_failed",
                onu=onu_external_id,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        return None

    # ------------------------------------------------------------------
    # Medição ativa — cara, sob evento, bate na OLT
    # ------------------------------------------------------------------

    def measure_now(self, *, onu_external_id: str) -> OpticalSignalDTO | None:
        """Dispara o botão de potência e devolve o que a OLT respondeu.

        O painel HTML já traz a potência, então o caminho principal é parseá-lo.
        A releitura da listagem existe como **plano B**: o botão também atualiza
        o registro no ERP (confirmado em produção — `data_sinal` saltou de 06:32
        para 21:03), então, se o formato do painel mudar, ainda dá para colher o
        valor pelo caminho normal em vez de perder a medição.

        `None` quando não houve leitura — inclusive quando a OLT não conhece mais
        a ONU (responde em 0,4 s, sem campo nenhum). Ausência é ausência.
        """
        if not onu_external_id:
            return None

        body: str | None = None
        try:
            with self._client_factory() as client:
                body = client.post_report_button(
                    MEASURE_RESOURCE, {"id": str(onu_external_id)}
                )
        except Exception as exc:
            # Do outro lado tem equipamento de produção: sem retry aqui. Quem
            # chama conta as leituras vazias e recua.
            _logger.warning(
                "ixc_onu_measure_failed",
                onu=onu_external_id,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            return None

        fields = onu_report_fields(body)
        measured = self._panel_to_dto(str(onu_external_id), fields)
        if measured is not None and measured.has_signal:
            return measured

        # Plano B: o botão também **atualiza** o registro no ERP (confirmado em
        # produção — `data_sinal` saltou de 06:32 para 21:03 no teste), então uma
        # mudança de formato no painel não precisa custar a medição. E o carimbo
        # do ERP é melhor que o nosso "agora": é a hora em que a OLT respondeu.
        fallback = self.get_optical_signal(onu_external_id=onu_external_id)
        if fallback is None or not fallback.has_signal:
            # Nem painel nem registro trouxeram potência. O que o painel disse
            # sobre causa e estado ainda é notícia e sobe assim mesmo.
            return measured if measured is not None else fallback
        if measured is None:
            return fallback
        return replace(
            fallback,
            run_state=measured.run_state,
            last_drop_cause=measured.last_drop_cause or fallback.last_drop_cause,
            last_up_at=measured.last_up_at,
            raw_extras={**fallback.raw_extras, **measured.raw_extras},
        )

    @staticmethod
    def _panel_to_dto(
        onu_external_id: str, fields: dict[str, Any]
    ) -> OpticalSignalDTO | None:
        """Campos do painel → DTO. `None` quando o painel não disse nada."""
        if not fields:
            return None
        return OpticalSignalDTO(
            onu_external_id=onu_external_id,
            signal_rx=fields.get("signal_rx"),
            signal_tx=fields.get("signal_tx"),
            # O painel não carimba a leitura, e o carimbo é o que separa "medi no
            # retorno" de "isto é a base de 18 horas atrás". Sem potência não há
            # o que carimbar: `measured_at` fica nulo e `has_signal` é falso.
            measured_at=_now() if fields.get("signal_rx") is not None else None,
            temperature=fields.get("temperature"),
            voltage=fields.get("voltage"),
            run_state=str(fields.get("run_state") or ""),
            last_drop_cause=str(fields.get("last_drop_cause") or ""),
            last_up_at=fields.get("last_up_time"),
            raw_extras={
                key: value
                for key, value in fields.items()
                if key in ("power_status", "ont_distance_m", "fsp", "control_flag")
            },
        )

    # ------------------------------------------------------------------
    # Tradução
    # ------------------------------------------------------------------

    @staticmethod
    def _listing_to_dto(raw: dict[str, Any]) -> OpticalSignalDTO | None:
        try:
            schema = IxcOnuSignalSchema.model_validate(raw)
        except ValidationError as exc:
            _logger.warning(
                "ixc_onu_signal_schema_invalid_skipped",
                external_id=raw.get("id"),
                errors=exc.errors()[:1],
            )
            return None
        if not schema.id:
            return None
        return OpticalSignalDTO(
            onu_external_id=schema.id,
            login_external_id=schema.id_login,
            signal_rx=schema.sinal_rx,
            signal_tx=schema.sinal_tx,
            measured_at=schema.data_sinal,
            temperature=schema.temperatura,
            voltage=schema.voltagem,
            # A listagem não diz o run state — só a medição ativa diz. Vazio aqui
            # é honesto: "não sabemos", e não "offline".
            last_drop_cause=schema.causa_ultima_queda,
            raw_extras=schema.get_extras(),
        )


def _now() -> datetime:
    from django.utils import timezone

    return timezone.now()
