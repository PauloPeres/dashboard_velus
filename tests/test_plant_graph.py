"""O grafo da planta — quem vem antes de quem, seguindo o cabo.

O que estes testes travam é a descoberta que fez o grafo funcionar: **a caixa
fica em cima do cabo, não na ponta dele**. Medido em produção (21/09/2026): 72%
das CTOs estão a menos de 5 m de um vértice, e quase nunca do primeiro ou do
último. Ligando só as pontas, o grafo alcançava 428 das 1.438 CTOs; cortando o
cabo nos elementos do caminho, alcança 949.

O segundo risco travado aqui é o do **sentido**. A ordem dos vértices no IXC não
diz para onde a luz anda (654 cabos começam mais perto do POP, 513 terminam),
então o sentido tem de sair do Dijkstra a partir do POP — nunca do desenho.
"""

from __future__ import annotations

from apps.network.domain.plant_graph import (
    CEO,
    CTO,
    POP,
    CableInput,
    NodeInput,
    build_graph,
    distances_from,
    route_to,
)

# Uma rua reta em Sorocaba, com ~100 m entre cada ponto: números redondos
# fazem a conta de metros ser conferível no olho.
_LON = -47.4500
_POP_LAT = -23.5000
_GRAU_POR_100M = 0.0009  # ~100 m de latitude


def _lat(passos: float) -> float:
    return _POP_LAT - _GRAU_POR_100M * passos


def _no(kind: str, ident: str, passos: float, lon: float = _LON) -> NodeInput:
    return NodeInput(kind=kind, external_id=ident, label=f"{kind} {ident}",
                     lat=_lat(passos), lon=lon)


class TestCabeCortadoNosElementos:
    def test_caixa_no_meio_do_cabo_vira_no_da_rota(self) -> None:
        """O caso que derrubou a primeira versão.

        Um cabo só, do POP ao fim da rua, com duas caixas no meio. Ligando só as
        pontas, as caixas do meio ficariam fora do grafo — que é exatamente o
        que acontecia com 70% da planta.
        """
        nodes = [
            _no(POP, "pop", 0),
            _no(CEO, "ceo1", 2),
            _no(CTO, "cto1", 4),
        ]
        cabo = CableInput(
            external_id="c1",
            name="FIBRA AS80 12FO BACKBONE",
            type_name="FIBRA AS80 12FO BACKBONE",
            points=tuple((_lat(p), _LON) for p in (0, 1, 2, 3, 4)),
        )
        grafo = build_graph(nodes, [cabo])

        assert grafo.cabos_usados == 1
        assert grafo.cabos_soltos == 0
        # Três elementos no mesmo cabo viram DOIS trechos, não um.
        assert {a.destino for a in grafo.vizinhos((POP, "pop"))} == {(CEO, "ceo1")}
        assert {a.destino for a in grafo.vizinhos((CEO, "ceo1"))} == {
            (POP, "pop"),
            (CTO, "cto1"),
        }

    def test_o_peso_e_o_cabo_e_nao_a_reta(self) -> None:
        """Um cabo que contorna o quarteirão anda mais que a reta entre as pontas."""
        nodes = [_no(POP, "pop", 0), _no(CTO, "cto1", 2)]
        # Desvio de ~100 m para o lado e volta: o traçado tem 3x a reta.
        cabo = CableInput(
            "c1", "desvio", "FIBRA",
            (
                (_lat(0), _LON),
                (_lat(0), _LON + _GRAU_POR_100M),
                (_lat(2), _LON + _GRAU_POR_100M),
                (_lat(2), _LON),
            ),
        )
        grafo = build_graph(nodes, [cabo])
        aresta = grafo.vizinhos((POP, "pop"))[0]
        reta = 200.0
        assert aresta.metros > reta * 1.5

    def test_cabo_que_nao_encosta_em_dois_elementos_e_contado_a_parte(self) -> None:
        """O grafo declara o que ficou de fora; some em silêncio seria pior."""
        nodes = [_no(POP, "pop", 0)]
        longe = CableInput(
            "c9", "cabo de outro bairro", "FIBRA",
            ((_lat(50), _LON), (_lat(51), _LON)),
        )
        grafo = build_graph(nodes, [longe])
        assert grafo.cabos_usados == 0
        assert grafo.cabos_soltos == 1

    def test_elemento_fora_da_tolerancia_nao_entra(self) -> None:
        """15 m é medido: colar caixa em cabo de outra rua inverte rotas."""
        nodes = [_no(POP, "pop", 0), _no(CTO, "longe", 1, lon=_LON + 0.0009)]
        cabo = CableInput("c1", "reta", "FIBRA",
                          ((_lat(0), _LON), (_lat(2), _LON)))
        grafo = build_graph(nodes, [cabo])
        assert grafo.vizinhos((CTO, "longe")) == []

    def test_o_pop_tem_alcance_proprio(self) -> None:
        """A coordenada do POP é o prédio; a fibra sai de uma caixa na calçada.

        Medido: com 15 m nenhum cabo encostava em POP nenhum.
        """
        nodes = [_no(POP, "pop", 0), _no(CTO, "cto1", 3)]
        # O cabo começa a ~100 m do POP — fora da tolerância normal.
        cabo = CableInput("c1", "saida", "FIBRA",
                          ((_lat(1), _LON), (_lat(2), _LON), (_lat(3), _LON)))
        grafo = build_graph(nodes, [cabo])
        assert grafo.vizinhos((POP, "pop")), "o POP ficou fora do grafo"


class TestSentidoVemDoPop:
    def _planta(self):
        nodes = [
            _no(POP, "pop", 0),
            _no(CEO, "ceo1", 2),
            _no(CEO, "ceo2", 4),
            _no(CTO, "cto1", 6),
        ]
        cabo = CableInput(
            "c1", "tronco", "FIBRA AS80 12FO BACKBONE",
            tuple((_lat(p), _LON) for p in (0, 1, 2, 3, 4, 5, 6)),
        )
        return build_graph(nodes, [cabo])

    def test_distancia_cresce_saindo_do_pop(self) -> None:
        grafo = self._planta()
        dist, _ = distances_from(grafo, [(POP, "pop")])
        assert dist[(CEO, "ceo1")] < dist[(CEO, "ceo2")] < dist[(CTO, "cto1")]

    def test_a_rota_sai_do_pop_e_chega_no_elemento(self) -> None:
        grafo = self._planta()
        dist, anterior = distances_from(grafo, [(POP, "pop")])
        rota = route_to(grafo, dist, anterior, (CTO, "cto1"))
        assert [h.node.external_id for h in rota] == ["pop", "ceo1", "ceo2", "cto1"]
        # O cabo por onde se chegou viaja junto: é o que o técnico procura.
        assert rota[-1].cabo_nome == "tronco"

    def test_elemento_sem_ligacao_nao_inventa_rota(self) -> None:
        """30% das CTOs estão nesse estado hoje. Lista vazia é a resposta honesta."""
        grafo = self._planta()
        dist, anterior = distances_from(grafo, [(POP, "pop")])
        assert route_to(grafo, dist, anterior, (CTO, "inexistente")) == []

    def test_a_ordem_dos_vertices_nao_define_o_sentido(self) -> None:
        """O mesmo cabo desenhado ao contrário dá a mesma rota.

        Medido no IXC: 654 cabos começam mais perto do POP e 513 terminam. Se o
        sentido saísse do desenho, metade das rotas sairia invertida.
        """
        nodes = [_no(POP, "pop", 0), _no(CEO, "ceo1", 2), _no(CTO, "cto1", 4)]
        pontos = tuple((_lat(p), _LON) for p in (0, 1, 2, 3, 4))
        direto = build_graph(nodes, [CableInput("c1", "t", "FIBRA", pontos)])
        invertido = build_graph(
            nodes, [CableInput("c1", "t", "FIBRA", tuple(reversed(pontos)))]
        )
        rotas = []
        for grafo in (direto, invertido):
            dist, anterior = distances_from(grafo, [(POP, "pop")])
            rotas.append([h.node.external_id for h in route_to(grafo, dist, anterior, (CTO, "cto1"))])
        assert rotas[0] == rotas[1] == ["pop", "ceo1", "cto1"]


class TestDrop:
    def test_a_aresta_sabe_que_e_drop(self) -> None:
        """O técnico pediu que o drop entre — mas saber que é drop muda o que
        ele espera achar no poste."""
        nodes = [_no(CEO, "ceo1", 0), _no(CTO, "cto1", 1)]
        cabo = CableInput("d1", "01FO", "CLIENTE DROP 1FO",
                          ((_lat(0), _LON), (_lat(1), _LON)))
        grafo = build_graph(nodes, [cabo])
        aresta = grafo.vizinhos((CEO, "ceo1"))[0]
        assert aresta.e_drop is True

    def test_tronco_nao_e_drop(self) -> None:
        nodes = [_no(CEO, "ceo1", 0), _no(CTO, "cto1", 1)]
        cabo = CableInput("c1", "FIBRA AS80 12FO", "FIBRA AS80 12FO BACKBONE",
                          ((_lat(0), _LON), (_lat(1), _LON)))
        grafo = build_graph(nodes, [cabo])
        assert grafo.vizinhos((CEO, "ceo1"))[0].e_drop is False
