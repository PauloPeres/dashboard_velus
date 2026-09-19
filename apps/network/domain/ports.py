"""Ports do bounded context Network — Protocols que adapters externos implementam."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from apps.integrations.shared.enums import Capability, SourceType

from .dto import (
    BandwidthUsageDTO,
    ConnectionDTO,
    ElementGeometryDTO,
    NetworkElementDTO,
    OpticalSignalDTO,
)


@runtime_checkable
class ConnectionSourcePort(Protocol):
    """Adapter que sabe ler estado de conexão (RADIUS) de algum sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_connections(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[ConnectionDTO]:
        """Itera conexões. since=None -> bootstrap; senão incremental."""
        ...

    def list_offline_connections(self) -> Iterator[ConnectionDTO]:
        """Itera só os logins ATIVOS que estão fora do ar agora (#144).

        Existe separado de `list_connections` porque o poll de 3 min não é um
        sync: ele não quer o universo, quer a lista curta de quem está fora
        (~240 linhas hoje, uma chamada). Quem voltou ao ar simplesmente some
        desta lista — é assim que o retorno é detectado.
        """
        ...

    def get_connection(self, external_id: str) -> ConnectionDTO | None:
        """Busca conexão única pelo ID na fonte externa."""
        ...


@runtime_checkable
class BandwidthUsageSourcePort(Protocol):
    """Adapter que sabe ler consumo de banda (accounting RADIUS) de um sistema externo."""

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_bandwidth_usage(
        self,
        *,
        since: datetime | None = None,
    ) -> Iterator[BandwidthUsageDTO]:
        """Itera registros de consumo. since=None -> bootstrap; senão incremental."""
        ...

    def get_bandwidth_usage(self, external_id: str) -> BandwidthUsageDTO | None:
        """Busca registro de consumo único pelo ID na fonte externa."""
        ...


@runtime_checkable
class NetworkElementSourcePort(Protocol):
    """Adapter que sabe ler a planta de rede (CTO, POP, PON, OLT, cabo).

    Sem `since`: a topologia é pequena (milhares de linhas) e muda devagar, e os
    endpoints de planta do IXC não expõem um last-modified confiável — o
    `ultima_atualizacao` das caixas vem zerado na maioria. Pull completo diário
    sai mais barato que um incremental que erra.
    """

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_network_elements(self) -> Iterator[NetworkElementDTO]:
        """Itera todos os elementos da planta."""
        ...

    def list_element_geometries(self) -> Iterator[ElementGeometryDTO]:
        """Itera os traçados da planta — os elementos que são linha, não ponto.

        Método separado, e não um campo do `NetworkElementDTO`, porque a
        geometria tem outro volume e outra origem: na fonte ela vem de recursos
        próprios, e um elemento pode ter dezenas de vértices. Fonte que não
        conheça traçado devolve vazio, e a planta sincroniza como sempre.
        """
        ...


@runtime_checkable
class OpticalSignalSourcePort(Protocol):
    """Adapter que sabe ler o sinal óptico e o estado da ONU de um login (#148).

    Port separado de `ConnectionSourcePort` porque as duas leituras têm custo e
    cadência incomparáveis, e misturá-las esconderia justamente o que precisa
    ficar visível:

    - `list_optical_signals` e `last_valid_signal_before` são **leitura passiva**
      do cadastro da ONU no ERP — baratas, mas a coleta que as alimenta é
      **diária** (a varredura das ~06:30 do IXC). Servem de **linha de base**,
      nunca de leitura pós-reparo: 4.551 das 4.554 leituras têm 6 a 24 horas;
    - `measure_now` é **medição ativa**: pede à OLT, ao vivo, a potência de uma
      ONU. Custa ~1,7 s (4,1 s no pior caso) e bate em equipamento de produção.

    Daí a regra de acionamento de `docs/massivas-plano.md` §2.8: o poll barato
    carrega o trabalho e a medição cara **só dispara quando um login volta**, uma
    vez por cliente. Sem massiva aberta e sem retorno, nenhuma chamada sai.

    Nenhum método levanta por falha da fonte: a leitura óptica é enriquecimento e
    não pode derrubar o poll de status, que é o dado principal. Ausência de
    leitura volta como `None` — que é **"não sei"**, e não "zero dBm".
    """

    source_type: SourceType
    capabilities: frozenset[Capability]

    def list_optical_signals(self) -> Iterator[OpticalSignalDTO]:
        """Itera a leitura corrente de todas as ONUs (uma listagem, sem tocar na OLT)."""
        ...

    def get_optical_signal(self, *, onu_external_id: str) -> OpticalSignalDTO | None:
        """Leitura corrente de uma ONU específica, pelo caminho de listagem."""
        ...

    def last_valid_signal_before(
        self, *, onu_external_id: str, before: datetime
    ) -> OpticalSignalDTO | None:
        """Última leitura **válida** (com potência) anterior a `before`.

        "Válida" é o ponto do método: o histórico está cheio de linhas com
        potência zerada — que é ausência de leitura, não -0 dBm. Pegar a última
        linha em vez da última linha *com sinal* faria todo cliente que caiu
        aparecer com ~24 dB de perda.
        """
        ...

    def measure_now(self, *, onu_external_id: str) -> OpticalSignalDTO | None:
        """Manda a OLT medir esta ONU agora. `None` = sem leitura.

        Funciona com a ONU fora do ar — é isso que permite comparar o sinal com
        que o cliente caiu e o sinal com que voltou. `None` cobre tanto a ONU que
        a OLT não conhece mais quanto a falha de leitura: nos dois casos o que
        temos é ausência, e ausência se guarda como ausência.
        """
        ...
