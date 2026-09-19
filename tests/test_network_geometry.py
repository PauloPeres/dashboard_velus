"""Geometria da planta: traçado do cabo e escolha do candidato.

O que estes testes protegem é a fronteira entre o que a geometria **prova** e o
que ela **sugere**. O traçado é cadastro do InMap: diz por onde o projeto passa
a fibra. Ele destrava dizer "estes cabos passam pelas caixas que caíram" — e não
destrava nada além disso.

Três regras vindas da medição de produção (2026-09-19, spike R2):

1. distância se mede contra o **segmento**, não contra o vértice: cabos têm
   mediana de 5 vértices e trechos longos entre eles;
2. **drop nunca é candidato** — é o pedaço que vai do poste à casa de um cliente
   e não explica trinta clientes fora;
3. caixa sem cabo por perto é **resposta**, não lista vazia: ~15% das caixas não
   têm traçado cadastrado a menos de 30 m.
"""

from __future__ import annotations

import pytest

from apps.network.domain.geometry import (
    RAIO_CANDIDATO_METROS,
    PathInput,
    cable_class,
    candidate_cables,
    ctos_sem_cabo,
    distance_to_path,
)

# Sorocaba, onde a planta real está: a projeção local depende da latitude.
LAT = -23.5
LON = -47.4
GRAU_POR_METRO_LAT = 1.0 / 111_132.0


def _ponto(metros_norte: float = 0.0, metros_leste: float = 0.0) -> tuple[float, float]:
    """Ponto deslocado do centro em metros — facilita ler as distâncias."""
    from math import cos, radians

    return (
        LAT + metros_norte * GRAU_POR_METRO_LAT,
        LON + metros_leste / (111_320.0 * cos(radians(LAT))),
    )


class TestDistanciaAteOTracado:
    def test_mede_contra_o_segmento_e_nao_so_contra_o_vertice(self) -> None:
        """Um cabo de dois vértices a 200 m passa rente a uma caixa no meio.

        Medindo só vértice, a resposta seria ~100 m e o cabo sairia da lista de
        candidatos — justamente o cabo que passa na porta da caixa.
        """
        cabo = [_ponto(metros_norte=-100), _ponto(metros_norte=100)]
        caixa = _ponto(metros_leste=2)
        assert distance_to_path(caixa, cabo) == pytest.approx(2.0, abs=0.3)

    def test_fora_do_segmento_a_distancia_e_ate_a_ponta(self) -> None:
        """Perpendicular que cai fora do trecho não vale: o cabo acaba ali."""
        cabo = [_ponto(), _ponto(metros_norte=100)]
        caixa = _ponto(metros_norte=150)
        assert distance_to_path(caixa, cabo) == pytest.approx(50.0, abs=0.5)

    def test_traçado_vazio_nao_fica_perto_de_nada(self) -> None:
        assert distance_to_path(_ponto(), []) == float("inf")

    def test_um_ponto_so_ainda_responde(self) -> None:
        """Elemento de um vértice é posição, não linha — mas medir dá."""
        assert distance_to_path(_ponto(metros_norte=10), [_ponto()]) == pytest.approx(
            10.0, abs=0.5
        )


class TestClasseDoCabo:
    def test_o_tipo_manda_sobre_o_nome(self) -> None:
        """O caso que motivou a mudança: 57 cabos de tipo "CLIENTE DROP 1FO" se
        chamam só "01FO". Lidos pelo nome, entravam como candidatos a explicar
        uma massiva de trinta clientes."""
        assert cable_class("01FO", "CLIENTE DROP 1FO") == "DROP"
        assert cable_class("FIBRA AS80 24FO 3", "FIBRA AS80 12FO BACKBONE") == "BACKBONE"

    def test_sem_tipo_cai_no_nome(self) -> None:
        """11 cabos de produção não têm tipo cadastrado — ali o nome ainda ajuda."""
        assert cable_class("FIBRA AS80 12FO BACKBONE 18") == "BACKBONE"
        assert cable_class("FIBRA AS80 12FO ATENDIMENTO 65") == "ATENDIMENTO"
        assert cable_class("CLIENTE DROP 1FO 30") == "DROP"

    def test_quando_nem_tipo_nem_nome_dizem_nao_inventa(self) -> None:
        """"FIBRA AS80 24FO" é quase certamente tronco pela capacidade — mas
        deduzir classe de capacidade é inferir onde o cadastro cala."""
        assert cable_class("CABO 24 F.O", "FIBRA AS80 24FO") == ""
        assert cable_class("", "") == ""


class TestCabosCandidatos:
    def test_cabo_que_toca_mais_caixas_vem_primeiro(self) -> None:
        """O cabo que costura três das caixas fora é a pista — mesmo que outro
        encoste mais perto de uma delas."""
        caixas = [_ponto(), _ponto(metros_norte=50), _ponto(metros_norte=100)]
        costura = PathInput(
            "1", "FIBRA AS80 12FO BACKBONE 18",
            [_ponto(metros_leste=1), _ponto(metros_norte=100, metros_leste=1)],
        )
        encosta = PathInput(
            "2", "FIBRA AS80 12FO ATENDIMENTO 65",
            [_ponto(), _ponto(metros_leste=40)],
        )
        candidatos = candidate_cables(caixas, [costura, encosta])
        assert [c.external_id for c in candidatos] == ["1", "2"]
        assert candidatos[0].ctos_tocadas == 3
        assert candidatos[0].classe == "BACKBONE"
        assert candidatos[1].ctos_tocadas == 1

    def test_drop_nunca_entra(self) -> None:
        """Drop é o pedaço que vai do poste à casa: não explica massiva, e
        listá-lo enterraria os cabos que importam."""
        caixas = [_ponto()]
        drop = PathInput("9", "CLIENTE DROP 1FO 30", [_ponto(), _ponto(metros_leste=5)])
        assert candidate_cables(caixas, [drop]) == []

    def test_drop_que_nao_se_declara_no_nome_tambem_fica_fora(self) -> None:
        """O defeito que o campo `type_name` corrigiu: em produção, 57 cabos de
        tipo drop se chamam apenas "01FO" e estavam entrando na lista."""
        caixas = [_ponto()]
        drop = PathInput(
            "9", "01FO", [_ponto(), _ponto(metros_leste=5)],
            type_name="CLIENTE DROP 1FO",
        )
        assert candidate_cables(caixas, [drop]) == []
        # Quem quiser ver o drop pede explicitamente.
        assert len(candidate_cables(caixas, [drop], incluir_drop=True)) == 1

    def test_cabo_longe_nao_e_candidato(self) -> None:
        caixas = [_ponto()]
        longe = PathInput(
            "3", "FIBRA BACKBONE 7",
            [_ponto(metros_leste=200), _ponto(metros_leste=300)],
        )
        assert candidate_cables(caixas, [longe]) == []

    def test_sem_caixa_ou_sem_tracado_nao_ha_candidato(self) -> None:
        cabo = PathInput("1", "FIBRA BACKBONE 1", [_ponto(), _ponto(metros_leste=10)])
        assert candidate_cables([], [cabo]) == []
        assert candidate_cables([_ponto()], []) == []

    def test_distancia_reportada_e_a_menor_ate_o_cabo(self) -> None:
        caixas = [_ponto(metros_leste=25), _ponto(metros_leste=5)]
        cabo = PathInput("1", "FIBRA BACKBONE 1", [_ponto(), _ponto(metros_norte=100)])
        candidato = candidate_cables(caixas, [cabo])[0]
        assert candidato.distance_meters == pytest.approx(5.0, abs=0.5)
        assert candidato.ctos_tocadas == 2


class TestCaixasSemCabo:
    def test_conta_as_caixas_que_ficaram_sem_candidato(self) -> None:
        """Lista curta de candidatos tem dois significados opostos — "achamos
        pouco" e "metade das caixas não tem cabo cadastrado". A contagem separa
        os dois, e é ela que vai para a tela."""
        perto = _ponto(metros_leste=5)
        longe = _ponto(metros_leste=500)
        cabo = PathInput("1", "FIBRA BACKBONE 1", [_ponto(), _ponto(metros_norte=100)])
        assert ctos_sem_cabo([perto, longe], [cabo]) == 1

    def test_sem_tracado_nenhum_todas_as_caixas_estao_sem_cabo(self) -> None:
        assert ctos_sem_cabo([_ponto(), _ponto(metros_norte=10)], []) == 2

    def test_raio_default_e_o_medido_em_producao(self) -> None:
        """30 m não é número redondo escolhido a esmo: a ≤30 m estão 1.211 das
        1.431 caixas, e o p90 (81,5 m) já traria o cabo de outra rua."""
        assert RAIO_CANDIDATO_METROS == 30.0
