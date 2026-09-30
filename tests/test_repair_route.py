"""Por onde o técnico começa, e onde deve estar o rompimento.

Estes testes existem porque a resposta errada aqui **manda um carro**. Três
afirmações que não podem escorregar:

1. a caixa de partida é a que melhor separa quem caiu de quem continua no ar.
   Se ainda há alguém no ar abaixo dela, o problema está mais para baixo, e
   mandar o técnico subir nela é uma subida perdida — mas **uma** caixa fora de
   lugar não pode jogar a resposta para o POP (massiva 359, 30/09/2026);
2. o rompimento fica **entre** a última caixa no ar e a primeira afetada. Sem
   caixa no ar antes, não há "entre" — e aí o sistema não marca X nenhum;
3. quando a rota não chega a um POP, o sistema não chama de "primeira" o que
   não sabe ser a primeira.
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
)
from apps.network.domain.repair_route import (
    calcular,
    ceos_da_rota,
    hipoteses_distintas,
    ranquear_hipoteses,
)

_LON = -47.4500
_LAT0 = -23.5000
_PASSO = 0.0009  # ~100 m


def _lat(p: float) -> float:
    return _LAT0 - _PASSO * p


def _no(kind: str, ident: str, passos: float, lon: float = _LON) -> NodeInput:
    return NodeInput(kind, ident, f"{kind} {ident}", _lat(passos), lon)


def _cabo(ident: str, passos, lon: float = _LON, tipo: str = "FIBRA AS80 12FO") -> CableInput:
    return CableInput(ident, f"cabo {ident}", tipo, tuple((_lat(p), lon) for p in passos))


def _planta():
    """POP → CEO1 → CEO2 → três CTOs, sendo uma pendurada na CEO1.

            pop
             |
           ceo1 ── cto_alta (ramo lateral)
             |
           ceo2 ── cto_a
             |
           cto_b
    """
    nodes = [
        _no(POP, "pop", 0),
        _no(CEO, "ceo1", 2),
        _no(CEO, "ceo2", 4),
        _no(CTO, "cto_alta", 2, lon=_LON + _PASSO),
        _no(CTO, "cto_a", 4, lon=_LON + _PASSO),
        _no(CTO, "cto_b", 6),
    ]
    cabos = [
        _cabo("tronco", (0, 1, 2, 3, 4, 5, 6)),
        CableInput("d1", "drop alta", "CLIENTE DROP 1FO",
                   ((_lat(2), _LON), (_lat(2), _LON + _PASSO))),
        CableInput("d2", "drop a", "CLIENTE DROP 1FO",
                   ((_lat(4), _LON), (_lat(4), _LON + _PASSO))),
    ]
    grafo = build_graph(nodes, cabos)
    dist, anterior = distances_from(grafo, [(POP, "pop")])
    return grafo, dist, anterior


TODAS = [(CTO, "cto_alta"), (CTO, "cto_a"), (CTO, "cto_b")]


class TestCaixaDePartida:
    def test_a_partida_e_a_primeira_caixa_com_tudo_fora(self) -> None:
        """Caiu tudo abaixo da CEO2; a CTO da CEO1 continua no ar.

        A resposta certa é CEO2: subir na CEO1 seria subida perdida, porque
        abaixo dela ainda há cliente no ar.
        """
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto_a"), (CTO, "cto_b")],
            ctos_conhecidas=TODAS,
        )
        assert rota.partida is not None
        assert rota.partida.external_id == "ceo2"
        assert rota.partida_status.tudo_fora is True
        assert rota.tem_origem_no_pop is True

    def test_a_caixa_anterior_entra_junto(self) -> None:
        """Pedido 9: sem a caixa de trás, o X flutua sobre o traçado."""
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto_a"), (CTO, "cto_b")],
            ctos_conhecidas=TODAS,
        )
        assert rota.caixa_anterior is not None
        assert rota.caixa_anterior.external_id == "ceo1"
        assert rota.trecho_rompido is not None
        de, para = rota.trecho_rompido
        assert (de.external_id, para.external_id) == ("ceo1", "ceo2")
        assert rota.cabo_rompido is not None
        assert rota.cabo_rompido.cabo_nome == "cabo tronco"

    def test_quem_caiu_e_quem_nao_por_caixa(self) -> None:
        """Pedido 1: é isto que dá confiança à caixa de partida."""
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto_a"), (CTO, "cto_b")],
            ctos_conhecidas=TODAS,
        )
        por_id = {c.node.external_id: c for c in ceos_da_rota(rota)}
        assert {n.external_id for n in por_id["ceo1"].ctos_fora} == {"cto_a", "cto_b"}
        assert {n.external_id for n in por_id["ceo1"].ctos_no_ar} == {"cto_alta"}
        assert por_id["ceo1"].tudo_fora is False
        assert {n.external_id for n in por_id["ceo2"].ctos_no_ar} == set()

    def test_a_primeira_cto_afetada_e_a_mais_perto_do_pop(self) -> None:
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto_b"), (CTO, "cto_a")],
            ctos_conhecidas=TODAS,
        )
        assert rota.primeira_cto is not None
        assert rota.primeira_cto.external_id == "cto_a"

    def test_caindo_tudo_desde_o_pop_nao_ha_trecho_e_nem_x(self) -> None:
        """Sem caixa no ar antes, não existe "entre" — e X chutado manda carro."""
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=TODAS,
            ctos_conhecidas=TODAS,
        )
        assert rota.partida is not None
        assert rota.partida.external_id in {"pop", "ceo1"}
        if rota.partida.external_id == "pop":
            assert rota.trecho_rompido is None


class TestSemPop:
    def test_sem_origem_no_pop_a_rota_nao_se_diz_primeira(self) -> None:
        """30% da planta está assim hoje: pedaço de desenho sem POP ligado."""
        nodes = [_no(CEO, "ceoA", 0), _no(CEO, "ceoB", 2), _no(CTO, "cto1", 4)]
        grafo = build_graph(nodes, [_cabo("solto", (0, 1, 2, 3, 4))])
        dist, anterior = distances_from(grafo, [(CEO, "ceoA")])
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto1")],
            ctos_conhecidas=[(CTO, "cto1")],
        )
        assert rota.completa is True
        assert rota.tem_origem_no_pop is False


class TestCobertura:
    def test_cto_fora_do_grafo_e_contada_e_nao_some(self) -> None:
        """O mapa é menor que a massiva, e a tela precisa poder dizer isso."""
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto_a"), (CTO, "fantasma")],
            ctos_conhecidas=TODAS,
        )
        assert rota.ctos_afetadas == 2
        assert rota.ctos_no_grafo == 1

    def test_nenhuma_cto_no_grafo_devolve_rota_vazia(self) -> None:
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "fantasma")],
            ctos_conhecidas=TODAS,
        )
        assert rota.completa is False
        assert rota.partida is None


class TestConfianca:
    """Quando o sistema NÃO pode dizer onde é o rompimento.

    Os dois casos vieram da validação contra a planta real (22/09/2026), e os
    dois apontavam trecho com a mesma cara de certeza de um caso bom.
    """

    def test_sem_caixa_com_tudo_fora_nao_afirma_trecho(self) -> None:
        """Caso real (#115): 2 CTOs fora e 35 no ar abaixo da mesma caixa.

        Duas CTOs caem em ramos diferentes da mesma CEO, e um terceiro ramo
        continua no ar. Não existe "primeira caixa afetada" aí — o que existe é
        o ponto comum das rotas. Dizer "rompimento aqui" mandaria o técnico
        subir num poste bom.
        """
        grafo, dist, anterior = _planta()
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=[(CTO, "cto_a"), (CTO, "cto_alta")],
            ctos_conhecidas=TODAS,
        )
        # O ponto comum é a CEO1, e abaixo dela ainda há cliente no ar.
        assert rota.partida is not None
        assert rota.partida.external_id == "ceo1"
        assert rota.partida_confirmada is False
        assert rota.trecho_rompido is None
        assert rota.partida_status.ctos_no_ar

    def test_cobertura_baixa_nao_afirma_trecho(self) -> None:
        """Caso real: 1 das 16 CTOs afetadas estava no grafo.

        Com o resto fora do desenho, "ainda no ar" pode conter gente que caiu e
        não foi mapeada — e o X apontaria para o lugar errado.
        """
        grafo, dist, anterior = _planta()
        afetadas = [(CTO, "cto_a"), (CTO, "cto_b")] + [
            (CTO, f"fantasma{i}") for i in range(10)
        ]
        rota = calcular(
            grafo, dist, anterior,
            ctos_afetadas=afetadas,
            ctos_conhecidas=TODAS,
        )
        assert rota.cobertura_suficiente is False
        assert rota.trecho_rompido is None
        # A rota continua servindo para orientar — o que some é a afirmação.
        assert rota.partida is not None


# ~30 m de longitude nesta latitude: mais que a tolerância de 15 m do grafo, então
# cada caixa vira um nó próprio sobre o cabo.
_P = 0.0003


def _planta_ramos(*, vazias_no_leste: int = 0):
    """Um tronco com três emendas, um ramo a oeste e um ramo longo a leste.

                    pop
                     |
        o3 - o2 - o1-ceo1
                     |
                   ceo2-l1-l2-...-l9[-v1-...-vN]
                     |
                   ceo3
                     |
                     s1

    As `v` são caixas **sem cliente** no fim do ramo leste.
    """
    nodes = [
        _no(POP, "pop", 0),
        _no(CEO, "ceo1", 2),
        _no(CEO, "ceo2", 4),
        _no(CEO, "ceo3", 6),
        _no(CTO, "s1", 8),
    ]
    nodes += [NodeInput(CTO, f"o{i}", f"CTO o{i}", _lat(2), _LON - _P * i) for i in range(1, 4)]
    nodes += [NodeInput(CTO, f"l{i}", f"CTO l{i}", _lat(4), _LON + _P * i) for i in range(1, 10)]
    nodes += [
        NodeInput(CTO, f"v{j}", f"CTO v{j}", _lat(4), _LON + _P * (9 + j))
        for j in range(1, vazias_no_leste + 1)
    ]
    cabos = [
        _cabo("tronco", (0, 1, 2, 3, 4, 5, 6, 7, 8)),
        CableInput("oeste", "cabo oeste", "FIBRA AS80 06FO",
                   tuple((_lat(2), _LON - _P * i) for i in range(4))),
        CableInput("leste", "cabo leste", "FIBRA AS80 12FO",
                   tuple((_lat(4), _LON + _P * i) for i in range(10 + vazias_no_leste))),
    ]
    grafo = build_graph(nodes, cabos)
    dist, anterior = distances_from(grafo, [(POP, "pop")])
    return grafo, dist, anterior


LESTE = [(CTO, f"l{i}") for i in range(1, 10)]
OESTE = [(CTO, f"o{i}") for i in range(1, 4)]


class TestMassiva359:
    """Os dois erros que a massiva 359 expôs (30/09/2026), em miniatura.

    Na 359 o "comece por aqui" foi parar no POP, a 1.155 m do rompimento: 13
    das 220 caixas fora não pendiam do cabo rompido, e 109 caixas SEM cliente
    abaixo dele contavam como "no ar".
    """

    def test_uma_caixa_fora_de_lugar_nao_joga_a_resposta_para_o_pop(self) -> None:
        """Nove caixas do ramo leste caem, e uma do oeste cai junto.

        A regra antiga exigia um ponto comum a TODAS: a do oeste puxava a
        resposta para a CEO1, acima dos dois ramos. A certa é o começo do ramo
        leste, que explica 9 das 10.
        """
        grafo, dist, anterior = _planta_ramos()
        fora = [*LESTE, (CTO, "o1")]
        no_ar = [(CTO, "o2"), (CTO, "o3"), (CTO, "s1")]
        rota = calcular(
            grafo, dist, anterior, ctos_afetadas=fora, ctos_conhecidas=fora + no_ar
        )
        assert rota.partida is not None
        assert rota.partida.external_id == "l1"
        assert rota.partida_confirmada is True
        assert rota.caixa_anterior is not None
        assert rota.caixa_anterior.external_id == "ceo2"
        assert rota.cabo_rompido is not None
        assert rota.cabo_rompido.cabo_id == "leste"

    def test_caixa_sem_cliente_nao_e_prova_de_que_a_fibra_esta_boa(self) -> None:
        """O ramo leste cai inteiro, e depois dele há 12 caixas vazias.

        Quem chama passa como conhecidas só quem tem cliente online — e aí o
        ramo se confirma. Se as vazias entrassem como "no ar", nenhuma caixa
        passaria na pureza e a resposta se perderia, como na 359.
        """
        grafo, dist, anterior = _planta_ramos(vazias_no_leste=12)
        no_ar = [*OESTE, (CTO, "s1")]
        rota = calcular(
            grafo, dist, anterior, ctos_afetadas=LESTE, ctos_conhecidas=LESTE + no_ar
        )
        assert rota.partida is not None
        assert rota.partida.external_id == "l1"
        assert rota.partida_confirmada is True

        vazias = [(CTO, f"v{j}") for j in range(1, 13)]
        com_vazias = calcular(
            grafo, dist, anterior,
            ctos_afetadas=LESTE, ctos_conhecidas=LESTE + no_ar + vazias,
        )
        assert com_vazias.partida_confirmada is False

    def test_as_hipoteses_vem_com_as_contas_e_uma_por_cabo(self) -> None:
        grafo, dist, anterior = _planta_ramos()
        fora = [*LESTE, (CTO, "o1")]
        no_ar = [(CTO, "o2"), (CTO, "o3"), (CTO, "s1")]
        rota = calcular(
            grafo, dist, anterior, ctos_afetadas=fora, ctos_conhecidas=fora + no_ar
        )
        primeira = rota.hipoteses[0]
        assert primeira.no.external_id == "l1"
        assert (primeira.fora_abaixo, primeira.fora_total, primeira.no_ar_abaixo) == (9, 10, 0)
        assert primeira.explica == 0.9
        assert primeira.pureza == 1.0
        assert primeira.forte is True
        cabos = [h.cabo_id or h.no.external_id for h in rota.hipoteses]
        assert len(cabos) == len(set(cabos)), "duas hipóteses no mesmo cabo"


class TestHipotesesFracas:
    def test_dois_lugares_ao_mesmo_tempo_nao_viram_um_trecho(self) -> None:
        """O ramo oeste inteiro e a caixa do fim do tronco, com o leste no ar.

        Nenhum ponto explica os dois lugares sem levar junto o ramo leste, que
        está no ar. A tela recebe as hipóteses, mas nenhuma é afirmada.
        """
        grafo, dist, anterior = _planta_ramos()
        fora = [*OESTE, (CTO, "s1")]
        rota = calcular(
            grafo, dist, anterior, ctos_afetadas=fora, ctos_conhecidas=fora + LESTE
        )
        assert rota.partida_confirmada is False
        assert rota.trecho_rompido is None
        assert rota.partida is not None
        assert rota.partida.external_id == "ceo1"
        assert rota.hipoteses
        assert not any(h.forte for h in rota.hipoteses)

    def test_empate_vai_para_o_mais_perto_do_pop(self) -> None:
        """Pedaços seguidos sem nada pendurado no meio têm a mesma nota.

        O técnico começa por cima — logo abaixo da última caixa no ar.
        """
        grafo, dist, anterior = _planta()
        ranking = ranquear_hipoteses(
            grafo, dist, anterior,
            fora=[(CTO, "cto_a"), (CTO, "cto_b")], no_ar=[(CTO, "cto_alta")],
        )
        assert ranking[0].no.external_id == "ceo2"
        assert hipoteses_distintas(ranking, 1) == [ranking[0]]
