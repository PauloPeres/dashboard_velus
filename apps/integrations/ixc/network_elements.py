"""IxcNetworkElementSource — implementação de NetworkElementSourcePort para IXC.

Cinco endpoints, uma planta:

| Endpoint | Kind | Volume | Pai |
|---|---|---|---|
| `radpop` | POP | 9 | — |
| `radpop_radio` | OLT | 3 | POP (`id_pop`) |
| `radpop_radio_porta_fibra` | PON | 307 | OLT (`id_pop_radio`) |
| `rad_caixa_ftth` | CTO | 1.445 | OLT (`id_transmissor`) |
| `df_elemento` (`tipo=CB`) | CABLE | 1.191 | — (só projeto) |

A CTO pendura na OLT e não na PON de propósito: `rad_caixa_ftth.id_interface`
(a porta PON da caixa) só está preenchido em 420 das 1.445 caixas, e a PON de
verdade é propriedade do login, não da caixa — ver `IxcOnuFibraSchema`.
`id_transmissor`, ao contrário, vem preenchido nas 1.445.

Quirk medido: `radpop_radio_porta_fibra` devolve a página HTML de erro do IXC
("Ocorreu um erro ao processar") quando recebe qualquer `qtype` — só aceita
paginação pura. Por isso este é o único recurso aqui sem filtro possível.

Cabo não tem geometria: `df_elemento_coordenada` só mapeia elemento → id de
coordenada e a tabela de coordenadas não tem endpoint (plano §2.3). Guardamos
nome e projeto; nada de traçado.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, ValidationError

from apps.integrations.shared.enums import Capability, SourceType
from apps.network.domain.dto import NetworkElementDTO

from .client import IxcHttpClient
from .schemas import (
    IxcCaixaFtthSchema,
    IxcDfElementoSchema,
    IxcPortaPonSchema,
    IxcRadPopRadioSchema,
    IxcRadPopSchema,
)

_logger = structlog.get_logger(__name__)


class IxcNetworkElementSource:
    """Adapter IXC para a capability NETWORK_ELEMENTS."""

    source_type: ClassVar[SourceType] = SourceType.IXC
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.NETWORK_ELEMENTS}
    )

    def __init__(self, *, base_url: str, user_id: str, api_token: str) -> None:
        self._client_factory = lambda: IxcHttpClient(
            base_url=base_url, user_id=user_id, api_token=api_token,
        )

    def list_network_elements(self) -> Iterator[NetworkElementDTO]:
        """Itera a planta inteira. Sem `since` — ver NetworkElementSourcePort."""
        with self._client_factory() as client:
            yield from self._collect(
                client, "radpop", IxcRadPopSchema, self._pop_to_dto
            )
            yield from self._collect(
                client, "radpop_radio", IxcRadPopRadioSchema, self._olt_to_dto
            )
            yield from self._collect(
                client,
                "radpop_radio_porta_fibra",
                IxcPortaPonSchema,
                self._pon_to_dto,
            )
            yield from self._collect(
                client, "rad_caixa_ftth", IxcCaixaFtthSchema, self._cto_to_dto
            )
            yield from self._collect(
                client,
                "df_elemento",
                IxcDfElementoSchema,
                self._cable_to_dto,
                # Sem o filtro por tipo viriam também postes, caixas de emenda e
                # o resto do inventário do InMap — 1.191 cabos num universo bem maior.
                body_filter={"qtype": "df_elemento.tipo", "query": "CB", "oper": "="},
            )

    def _collect(
        self,
        client: IxcHttpClient,
        resource: str,
        schema_cls: type[BaseModel],
        to_dto: Any,
        *,
        body_filter: dict[str, str] | None = None,
    ) -> Iterator[NetworkElementDTO]:
        """Pagina um endpoint, valida (ACL) e traduz. Linha inválida é pulada com log."""
        skipped = 0
        total = 0
        for raw in client.paginate_ixc(resource, body_filter=body_filter):
            try:
                schema = schema_cls.model_validate(raw)
            except ValidationError as exc:
                skipped += 1
                _logger.warning(
                    "ixc_network_element_schema_invalid_skipped",
                    resource=resource,
                    external_id=raw.get("id"),
                    errors=exc.errors()[:1],
                )
                continue
            dto = to_dto(schema)
            if dto is not None:
                total += 1
                yield dto
        _logger.info(
            "ixc_network_element_resource_done",
            resource=resource,
            emitted=total,
            skipped=skipped,
        )

    # -------------------------------------------------------------------------
    # Tradutores — um por endpoint
    # -------------------------------------------------------------------------
    @staticmethod
    def _pop_to_dto(schema: IxcRadPopSchema) -> NetworkElementDTO:
        return NetworkElementDTO(
            external_id=schema.id,
            kind="POP",
            name=schema.pop,
            latitude=schema.latitude,
            longitude=schema.longitude,
            address=_join_address(schema.endereco, schema.numero, schema.bairro),
            project_external_id=schema.id_projeto,
            status=schema.tp_estacao,
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _olt_to_dto(schema: IxcRadPopRadioSchema) -> NetworkElementDTO:
        return NetworkElementDTO(
            external_id=schema.id,
            kind="OLT",
            name=schema.descricao,
            parent_external_id=schema.id_pop,
            parent_kind="POP" if schema.id_pop else "",
            status=schema.ativo,
            # `raw_extras` montado à mão (o schema é `extra="ignore"`): o registro
            # da OLT traz senhas de gerência do equipamento, que não têm por que
            # existir no banco de um dashboard read-only.
            raw_extras={
                "modelo": schema.modelo,
                "fabricante_modelo": schema.fabricante_modelo,
            },
        )

    @staticmethod
    def _pon_to_dto(schema: IxcPortaPonSchema) -> NetworkElementDTO:
        # `interface` ("0/2/15") é o nome que a operação usa no CLI da OLT;
        # quando falta, o número da PON é o melhor rótulo disponível.
        name = schema.interface or f"PON {schema.numero_pon}"
        return NetworkElementDTO(
            external_id=schema.id,
            kind="PON",
            name=name,
            parent_external_id=schema.id_pop_radio,
            parent_kind="OLT" if schema.id_pop_radio else "",
            raw_extras=schema.get_extras(),
        )

    @staticmethod
    def _cto_to_dto(schema: IxcCaixaFtthSchema) -> NetworkElementDTO:
        return NetworkElementDTO(
            external_id=schema.id,
            kind="CTO",
            name=schema.descricao,
            latitude=schema.latitude,
            longitude=schema.longitude,
            parent_external_id=schema.id_transmissor,
            parent_kind="OLT" if schema.id_transmissor else "",
            capacity=schema.capacidade or None,
            address=_join_address(schema.endereco, schema.numero, schema.bairro),
            project_external_id=schema.id_projeto,
            status=schema.status,
            # `bairro`, `id_cidade`, `cep` e `tipo` são campos declarados no
            # schema (logo, fora de `model_extra`) mas o DTO neutro não tem
            # lugar pra eles — vão explícitos pro `raw_extras`, que é onde o
            # resumo de CTOs por bairro os procura.
            raw_extras={
                **schema.get_extras(),
                "bairro": schema.bairro,
                "id_cidade": schema.id_cidade,
                "cep": schema.cep,
                "tipo": schema.tipo,
            },
        )

    @staticmethod
    def _cable_to_dto(schema: IxcDfElementoSchema) -> NetworkElementDTO:
        return NetworkElementDTO(
            external_id=schema.id,
            kind="CABLE",
            name=schema.descricao,
            project_external_id=schema.id_projeto,
            raw_extras=schema.get_extras(),
        )


def _join_address(endereco: str, numero: str, bairro: str) -> str:
    """Endereço legível a partir dos 3 campos do IXC, sem vírgulas órfãs."""
    rua = f"{endereco}, {numero}" if endereco and numero else endereco
    partes = [p for p in (rua, bairro) if p]
    return " - ".join(partes)[:255]
