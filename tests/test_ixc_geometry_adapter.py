"""Adapter IXC: montagem do traçado do cabo a partir de três recursos.

O traçado não vem pronto de lugar nenhum — é `df_elemento` (o cabo),
`df_elemento_coordenada` (o vínculo, com a ordem no campo `sequencia`) e
`df_coordenada` (o ponto). O que estes testes protegem:

1. **a ordem é o dado.** Os vínculos voltam da API em ordem de gravação (id
   desc, como no spike: sequencia 1, 2, 0). Montar na ordem recebida desenha um
   cabo em zigue-zague que atravessa a cidade;
2. **cabo sem ponto não vira DTO.** Traçado vazio não é traçado, e gravá-lo faria
   a tela desenhar nada achando que desenhou algo;
3. **coordenada quebrada não derruba o cabo inteiro** — nem vira (0, 0), que
   cairia no golfo da Guiné.
"""

from __future__ import annotations

from typing import Any

import respx
from httpx import Response

from apps.integrations.ixc.network_elements import IxcNetworkElementSource
from apps.network.domain.dto import ElementGeometryDTO

BASE_URL = "https://erp.test.com.br"
API_URL = f"{BASE_URL}/webservice/v1"


def _pagina(registros: list[dict[str, Any]]) -> Response:
    return Response(200, json={"page": "1", "total": str(len(registros)), "registros": registros})


def _fonte() -> IxcNetworkElementSource:
    return IxcNetworkElementSource(base_url=BASE_URL, user_id="1", api_token="t")


def _sem_catalogo_de_tipos(respx_mock: respx.MockRouter) -> None:
    """O adapter sempre lê `df_tipo_elemento`; aqui ele volta vazio.

    Nos testes que olham só a montagem da polilinha, o catálogo não é o assunto
    — mas a chamada existe, e sem o mock o respx recusa a requisição.
    """
    respx_mock.get(f"{API_URL}/df_tipo_elemento").mock(return_value=_pagina([]))


class TestTracadoDoCabo:
    def test_monta_a_polilinha_na_ordem_da_sequencia(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Os vínculos chegam fora de ordem (é assim que a API devolve)."""
        _sem_catalogo_de_tipos(respx_mock)
        respx_mock.get(f"{API_URL}/df_coordenada").mock(
            return_value=_pagina([
                {"id": "10", "latitude": "-23.5000", "longitude": "-47.4000"},
                {"id": "11", "latitude": "-23.5010", "longitude": "-47.4010"},
                {"id": "12", "latitude": "-23.5020", "longitude": "-47.4020"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([
                {"id": "3", "id_elemento": "2808", "id_coordenada": "11", "sequencia": "1"},
                {"id": "2", "id_elemento": "2808", "id_coordenada": "12", "sequencia": "2"},
                {"id": "1", "id_elemento": "2808", "id_coordenada": "10", "sequencia": "0"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento").mock(
            return_value=_pagina([
                {
                    "id": "2808",
                    "descricao": "FIBRA AS80 12FO ATENDIMENTO 104",
                    "id_projeto": "7",
                    "tipo": "CB",
                }
            ])
        )

        # O adapter pede `df_elemento` uma vez por tipo (CB para cabo, CA para
        # caixa de emenda) e o mock responde o mesmo para os dois — por isso o
        # filtro por kind aqui, em vez de assumir uma geometria só.
        geometrias = [
            g for g in _fonte().list_element_geometries() if g.kind == "CABLE"
        ]

        assert len(geometrias) == 1
        geo = geometrias[0]
        assert isinstance(geo, ElementGeometryDTO)
        assert geo.external_id == "2808"
        assert geo.kind == "CABLE"
        assert geo.name == "FIBRA AS80 12FO ATENDIMENTO 104"
        assert geo.project_external_id == "7"
        # Ordem de sequencia (0, 1, 2), não a ordem em que os vínculos vieram.
        assert geo.points == (
            (-23.5000, -47.4000),
            (-23.5010, -47.4010),
            (-23.5020, -47.4020),
        )
        assert geo.is_line is True

    def test_cabo_sem_ponto_nao_vira_dto(self, respx_mock: respx.MockRouter) -> None:
        _sem_catalogo_de_tipos(respx_mock)
        respx_mock.get(f"{API_URL}/df_coordenada").mock(return_value=_pagina([]))
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([])
        )
        respx_mock.get(f"{API_URL}/df_elemento").mock(
            return_value=_pagina([
                {"id": "999", "descricao": "CABO SEM GEO", "tipo": "CB"}
            ])
        )

        assert list(_fonte().list_element_geometries()) == []

    def test_coordenada_ilegivel_some_sem_levar_o_cabo(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Um ponto quebrado não pode virar (0, 0) nem apagar o cabo inteiro."""
        _sem_catalogo_de_tipos(respx_mock)
        _sem_catalogo_de_tipos(respx_mock)
        respx_mock.get(f"{API_URL}/df_coordenada").mock(
            return_value=_pagina([
                {"id": "10", "latitude": "-23.5000", "longitude": "-47.4000"},
                {"id": "11", "latitude": "", "longitude": ""},
                {"id": "12", "latitude": "-23.5020", "longitude": "-47.4020"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([
                {"id": "1", "id_elemento": "5", "id_coordenada": "10", "sequencia": "0"},
                {"id": "2", "id_elemento": "5", "id_coordenada": "11", "sequencia": "1"},
                {"id": "3", "id_elemento": "5", "id_coordenada": "12", "sequencia": "2"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento").mock(
            return_value=_pagina([{"id": "5", "descricao": "FIBRA BACKBONE 3", "tipo": "CB"}])
        )

        geo = next(g for g in _fonte().list_element_geometries() if g.kind == "CABLE")
        assert geo.points == ((-23.5000, -47.4000), (-23.5020, -47.4020))

    def test_coordenada_em_zero_zero_e_ausencia_disfarcada(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """(0, 0) cairia no golfo da Guiné e entraria em cluster geográfico."""
        _sem_catalogo_de_tipos(respx_mock)
        respx_mock.get(f"{API_URL}/df_coordenada").mock(
            return_value=_pagina([
                {"id": "10", "latitude": "0", "longitude": "0"},
                {"id": "11", "latitude": "-23.5010", "longitude": "-47.4010"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([
                {"id": "1", "id_elemento": "5", "id_coordenada": "10", "sequencia": "0"},
                {"id": "2", "id_elemento": "5", "id_coordenada": "11", "sequencia": "1"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento").mock(
            return_value=_pagina([{"id": "5", "descricao": "FIBRA BACKBONE 3", "tipo": "CB"}])
        )

        geo = next(g for g in _fonte().list_element_geometries() if g.kind == "CABLE")
        assert geo.points == ((-23.5010, -47.4010),)
        assert geo.is_line is False


class TestClasseVemDoTipo:
    def test_o_tipo_do_catalogo_acompanha_o_tracado(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Sem o `nome_tipo`, um cabo chamado "01FO" não se declara drop — e 57
        deles, em produção, são drop de cliente."""
        respx_mock.get(f"{API_URL}/df_coordenada").mock(
            return_value=_pagina([
                {"id": "10", "latitude": "-23.5000", "longitude": "-47.4000"},
                {"id": "11", "latitude": "-23.5010", "longitude": "-47.4010"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([
                {"id": "1", "id_elemento": "5", "id_coordenada": "10", "sequencia": "0"},
                {"id": "2", "id_elemento": "5", "id_coordenada": "11", "sequencia": "1"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_tipo_elemento").mock(
            return_value=_pagina([
                {"id": "8", "nome_tipo": "CLIENTE DROP 1FO"},
                {"id": "72", "nome_tipo": "FIBRA AS80 12FO BACKBONE"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento").mock(
            return_value=_pagina([
                {"id": "5", "descricao": "01FO", "tipo": "CB", "id_tipo_elemento": "8"}
            ])
        )

        geo = next(g for g in _fonte().list_element_geometries() if g.kind == "CABLE")
        assert geo.name == "01FO"
        assert geo.type_name == "CLIENTE DROP 1FO"

    def test_cabo_sem_tipo_no_catalogo_nao_quebra(
        self, respx_mock: respx.MockRouter
    ) -> None:
        respx_mock.get(f"{API_URL}/df_coordenada").mock(
            return_value=_pagina([
                {"id": "10", "latitude": "-23.5", "longitude": "-47.4"},
                {"id": "11", "latitude": "-23.501", "longitude": "-47.401"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([
                {"id": "1", "id_elemento": "5", "id_coordenada": "10", "sequencia": "0"},
                {"id": "2", "id_elemento": "5", "id_coordenada": "11", "sequencia": "1"},
            ])
        )
        respx_mock.get(f"{API_URL}/df_tipo_elemento").mock(return_value=_pagina([]))
        respx_mock.get(f"{API_URL}/df_elemento").mock(
            return_value=_pagina([
                {"id": "5", "descricao": "FIBRA BACKBONE 3", "tipo": "CB", "id_tipo_elemento": "0"}
            ])
        )

        geo = next(g for g in _fonte().list_element_geometries() if g.kind == "CABLE")
        assert geo.type_name == ""


class TestCaixaDeEmenda:
    def test_emenda_vira_geometria_de_um_ponto(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """A caixa de emenda (tipo CA) é onde o cabo é aberto. Vem com um ponto
        só — é posição, não traçado — e por isso `is_line` é falso."""
        respx_mock.get(f"{API_URL}/df_tipo_elemento").mock(
            return_value=_pagina([{"id": "43", "nome_tipo": "CAIXA DE EMENDA 24FO"}])
        )
        respx_mock.get(f"{API_URL}/df_coordenada").mock(
            return_value=_pagina([
                {"id": "10", "latitude": "-23.6068", "longitude": "-47.4890"}
            ])
        )
        respx_mock.get(f"{API_URL}/df_elemento_coordenada").mock(
            return_value=_pagina([
                {"id": "1", "id_elemento": "2812", "id_coordenada": "10", "sequencia": "0"}
            ])
        )

        def por_tipo(request: Any) -> Response:
            corpo = request.content.decode()
            if '"query": "CA"' in corpo:
                return _pagina([
                    {
                        "id": "2812",
                        "descricao": "Caixa de Emenda Vermelha 142",
                        "tipo": "CA",
                        "id_tipo_elemento": "43",
                    }
                ])
            return _pagina([])

        respx_mock.get(f"{API_URL}/df_elemento").mock(side_effect=por_tipo)

        emendas = [
            g for g in _fonte().list_element_geometries() if g.kind == "SPLICE"
        ]
        assert len(emendas) == 1
        assert emendas[0].name == "Caixa de Emenda Vermelha 142"
        assert emendas[0].points == ((-23.6068, -47.4890),)
        assert emendas[0].is_line is False


class TestSplittersEPostes:
    """Splitter e poste também vêm do projeto (pedido do operador, 22/09/2026).

    O splitter importa porque é ele que diz quantos clientes pendem de uma
    caixa; o poste, porque é o único objeto do desenho que o técnico enxerga da
    rua. Ambos são elementos de UM ponto, como a caixa de emenda — não linha.
    """

    def test_os_tipos_do_projeto_incluem_splitter_e_poste(self) -> None:
        from apps.integrations.ixc.network_elements import IxcNetworkElementSource

        tipos = IxcNetworkElementSource._GEOMETRY_TYPES
        assert tipos["SP"] == "SPLITTER"
        assert tipos["PT"] == "POLE"
        # E o cabo continua sendo o único que vira linha.
        assert tipos["CB"] == "CABLE"

    def test_o_dominio_aceita_os_novos_tipos(self) -> None:
        from apps.network.domain.dto import NETWORK_ELEMENT_KINDS

        assert "SPLITTER" in NETWORK_ELEMENT_KINDS
        assert "POLE" in NETWORK_ELEMENT_KINDS
