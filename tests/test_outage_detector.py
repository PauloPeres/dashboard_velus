"""Testes do detector de massivas (`apps.network.domain.outage`).

Domínio puro: nada de banco, nada de Django. Os números de topologia imitam a
medição real do IXC de 2026-09-08 descrita em `docs/massivas-plano.md`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.network.domain.outage import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    SCOPE_CTO,
    SCOPE_GEO,
    SCOPE_OLT,
    SCOPE_PON,
    SCOPE_POP,
    DropInput,
    OutageCluster,
    TopologyInput,
    detect_outages,
)

T0 = datetime(2026, 9, 8, 19, 46, tzinfo=UTC)

# ~1 grau de latitude ≈ 111.320 m; 0,0001 grau ≈ 11 m.
DEG_PER_METER = 1.0 / 111_320.0


def drop(
    login_id: str,
    *,
    minutes: float = 0.0,
    cto: str = "",
    pon: str = "",
    transmitter: str = "",
    pop: str = "",
    lat: float | None = None,
    lon: float | None = None,
    mrr: str = "100.00",
) -> DropInput:
    return DropInput(
        login_id=login_id,
        dropped_at=T0 + timedelta(minutes=minutes),
        cto_id=cto,
        cto_port="1",
        transmitter_id=transmitter,
        pon_id=pon,
        pop_id=pop,
        latitude=lat,
        longitude=lon,
        reason="",
        monthly_amount=Decimal(mrr),
    )


def cto_drops(
    cto: str,
    count: int,
    *,
    pon: str = "",
    transmitter: str = "",
    pop: str = "",
    minutes: float = 0.0,
    mrr: str = "100.00",
) -> list[DropInput]:
    return [
        drop(
            f"{cto}-{i}",
            minutes=minutes + i * 0.1,
            cto=cto,
            pon=pon,
            transmitter=transmitter,
            pop=pop,
            mrr=mrr,
        )
        for i in range(count)
    ]


def topology(**kwargs: object) -> TopologyInput:
    return TopologyInput(**kwargs)  # type: ignore[arg-type]


def only(clusters: list[OutageCluster]) -> OutageCluster:
    assert len(clusters) == 1, [(c.scope, c.element_id, c.affected_count) for c in clusters]
    return clusters[0]


# ---------------------------------------------------------------------------
# Escopo CTO
# ---------------------------------------------------------------------------


def test_cto_inteira_fora_vira_massiva_de_cto():
    drops = cto_drops("CTO-A", 6, transmitter="T1")
    result = detect_outages(
        drops,
        topology(
            active_logins_per_cto={"CTO-A": 6},
            cto_to_transmitter={"CTO-A": "T1"},
            cto_names={"CTO-A": "B41-SP03"},
        ),
    )
    cluster = only(result)
    assert cluster.scope == SCOPE_CTO
    assert cluster.element_id == "CTO-A"
    assert cluster.element_label == "B41-SP03"
    assert cluster.confidence == CONFIDENCE_HIGH
    assert cluster.affected_count == 6
    assert cluster.affected_fraction == pytest.approx(1.0)
    assert cluster.started_at == T0
    assert cluster.suspected_segment_label == ""


def test_cto_parcial_abaixo_do_limiar_nao_vira_massiva():
    # 6 de 12 = 50%, abaixo dos 70% — sem coordenada, também não vira geográfico.
    result = detect_outages(
        cto_drops("CTO-A", 6, transmitter="T1"),
        topology(active_logins_per_cto={"CTO-A": 12}, cto_to_transmitter={"CTO-A": "T1"}),
    )
    assert result == []


def test_cto_acima_do_limiar_mas_parcial_fica_com_confianca_media():
    # 8 de 10 = 80%: fecha o escopo (≥70%) mas não chega aos 90% da confiança alta.
    result = detect_outages(
        cto_drops("CTO-A", 8, transmitter="T1"),
        topology(active_logins_per_cto={"CTO-A": 10}, cto_to_transmitter={"CTO-A": "T1"}),
    )
    cluster = only(result)
    assert cluster.scope == SCOPE_CTO
    assert cluster.confidence == CONFIDENCE_MEDIUM
    assert cluster.affected_fraction == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Denominador mínimo da CTO (correção vinda do dado de produção)
# ---------------------------------------------------------------------------


def test_ctos_de_um_ou_dois_logins_nao_sustentam_escopo():
    # 511 das 852 CTOs de produção têm ≤2 logins: 100% fora ali é aritmética,
    # não evidência. Sem coordenada, nada deve sair.
    drops = [
        drop(f"login-{i}", minutes=i * 0.1, cto=f"CTO-{i}", transmitter="T1") for i in range(8)
    ]
    result = detect_outages(
        drops,
        topology(
            active_logins_per_cto={f"CTO-{i}": 1 for i in range(8)},
            cto_to_transmitter={f"CTO-{i}": "T1" for i in range(8)},
        ),
    )
    assert result == []


def test_min_cto_denominator_configuravel_reabilita_caixas_pequenas():
    drops = cto_drops("CTO-A", 2, transmitter="T1")
    topo = topology(active_logins_per_cto={"CTO-A": 2}, cto_to_transmitter={"CTO-A": "T1"})
    assert detect_outages(drops, topo, min_clients=2) == []
    cluster = only(detect_outages(drops, topo, min_clients=2, min_cto_denominator=1))
    assert cluster.scope == SCOPE_CTO
    assert cluster.affected_fraction == pytest.approx(1.0)
    # Afrouxar o escopo é decisão do operador; ALTA confiança sobre 2 logins não.
    assert cluster.confidence == CONFIDENCE_MEDIUM


def test_logins_de_cto_pequena_contam_no_cluster_sem_virar_evidencia():
    drops = (
        cto_drops("CTO-A", 3, pon="P1", transmitter="T1")
        + cto_drops("CTO-B", 3, pon="P2", transmitter="T1", minutes=1)
        + [drop("solo-1", minutes=2, cto="CTO-MINI", pon="P3", transmitter="T1", mrr="50.00")]
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3, "CTO-MINI": 1},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1", "CTO-MINI": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.affected_count == 7
    assert "solo-1" in cluster.login_ids
    assert cluster.mrr_at_risk == Decimal("650.00")


# ---------------------------------------------------------------------------
# Escalada PON / OLT / POP
# ---------------------------------------------------------------------------


def test_duas_ctos_da_mesma_pon_escalam_para_escopo_pon():
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 3, pon="P1", transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
                cto_names={"CTO-A": "CX-01", "CTO-B": "CX-02"},
            ),
        )
    )
    assert cluster.scope == SCOPE_PON
    assert cluster.element_id == "P1"
    assert cluster.element_label == "PON P1"
    assert cluster.affected_count == 6
    assert cluster.confidence == CONFIDENCE_HIGH


def test_pon_vem_do_login_e_nao_da_caixa():
    # A mesma caixa pode ser alimentada por duas portas (26% das CTOs de
    # produção). A filiação é pela porta majoritária entre as quedas da caixa.
    drops = [
        drop("a-1", cto="CTO-A", pon="P1", transmitter="T1"),
        drop("a-2", minutes=0.1, cto="CTO-A", pon="P1", transmitter="T1"),
        drop("a-3", minutes=0.2, cto="CTO-A", pon="P9", transmitter="T1"),
        *cto_drops("CTO-B", 3, pon="P1", transmitter="T1", minutes=1),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
                # Cadastro diz outra coisa — e é ignorado de propósito.
                cto_to_pon={"CTO-A": "P7", "CTO-B": "P7"},
            ),
        )
    )
    assert cluster.scope == SCOPE_PON
    assert cluster.element_id == "P1"
    assert cluster.affected_count == 6


def test_escala_para_olt_com_duas_pons_do_mesmo_transmissor():
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 3, pon="P2", transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.element_id == "T1"
    assert cluster.element_label == "OLT T1"
    assert cluster.affected_count == 6


def test_olt_fecha_mesmo_com_ctos_geograficamente_distantes():
    # Caso real das 19:46: caixas espalhadas por ~1.880 m. O raio geográfico de
    # 300 m jamais as uniria; o que as une é o transmissor comum.
    far = 1880 * DEG_PER_METER
    drops = [
        *cto_drops("CTO-A", 3, pon="P1", transmitter="T1"),
        *cto_drops("CTO-B", 3, pon="P2", transmitter="T1", minutes=1),
    ]
    drops = [
        replace(
            d,
            latitude=-23.5 + (far if d.cto_id == "CTO-B" else 0.0),
            longitude=-46.6,
        )
        for d in drops
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.affected_count == 6


def test_escala_para_pop_com_duas_olts():
    drops = [
        *cto_drops("CTO-A", 3, pon="P1", transmitter="T1", pop="POP1"),
        *cto_drops("CTO-B", 3, pon="P2", transmitter="T1", pop="POP1", minutes=1),
        *cto_drops("CTO-C", 3, pon="P3", transmitter="T2", pop="POP1", minutes=2),
        *cto_drops("CTO-D", 3, pon="P4", transmitter="T2", pop="POP1", minutes=3),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3, "CTO-C": 3, "CTO-D": 3},
                cto_to_transmitter={
                    "CTO-A": "T1",
                    "CTO-B": "T1",
                    "CTO-C": "T2",
                    "CTO-D": "T2",
                },
                cto_to_pop=dict.fromkeys(["CTO-A", "CTO-B", "CTO-C", "CTO-D"], "POP1"),
            ),
        )
    )
    assert cluster.scope == SCOPE_POP
    assert cluster.element_id == "POP1"
    assert cluster.affected_count == 12
    assert cluster.confidence == CONFIDENCE_HIGH


def test_pon_vazia_degrada_para_olt_sem_quebrar():
    # `pon_id` vem "" numa fração dos logins; a caixa em escopo vira a unidade
    # de evidência e o degrau de OLT ainda fecha.
    drops = cto_drops("CTO-A", 3, transmitter="T1") + cto_drops(
        "CTO-B", 3, transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.affected_count == 6


# ---------------------------------------------------------------------------
# Janela de tempo
# ---------------------------------------------------------------------------


def test_quedas_espalhadas_no_tempo_nao_agrupam():
    drops = [drop(f"login-{i}", minutes=i * 20, cto="CTO-A", transmitter="T1") for i in range(6)]
    result = detect_outages(
        drops,
        topology(active_logins_per_cto={"CTO-A": 6}, cto_to_transmitter={"CTO-A": "T1"}),
    )
    assert result == []


def test_gotejamento_dentro_do_intervalo_nao_encadeia_janela_infinita():
    # Uma queda a cada 4 min: encadear por gap juntaria tudo. A janela é ancorada
    # na primeira queda, então só as que cabem em 10 min entram.
    drops = [drop(f"login-{i}", minutes=i * 4, cto="CTO-A", transmitter="T1") for i in range(6)]
    result = detect_outages(
        drops,
        topology(active_logins_per_cto={"CTO-A": 6}, cto_to_transmitter={"CTO-A": "T1"}),
    )
    assert result == []


def test_janela_configuravel_agrupa_quedas_mais_espacadas():
    drops = [drop(f"login-{i}", minutes=i * 4, cto="CTO-A", transmitter="T1") for i in range(6)]
    cluster = only(
        detect_outages(
            drops,
            topology(active_logins_per_cto={"CTO-A": 6}, cto_to_transmitter={"CTO-A": "T1"}),
            window_minutes=30,
        )
    )
    assert cluster.affected_count == 6


# ---------------------------------------------------------------------------
# Limiar de clientes
# ---------------------------------------------------------------------------


def test_borda_exata_do_min_clients():
    topo = topology(active_logins_per_cto={"CTO-A": 5}, cto_to_transmitter={"CTO-A": "T1"})
    cluster = only(detect_outages(cto_drops("CTO-A", 5, transmitter="T1"), topo))
    assert cluster.affected_count == 5


def test_um_abaixo_do_min_clients_nao_vira_massiva():
    result = detect_outages(
        cto_drops("CTO-A", 4, transmitter="T1"),
        topology(active_logins_per_cto={"CTO-A": 4}, cto_to_transmitter={"CTO-A": "T1"}),
    )
    assert result == []


# ---------------------------------------------------------------------------
# Cluster geográfico
# ---------------------------------------------------------------------------


def test_cluster_geografico_sem_topologia():
    drops = [
        drop(f"geo-{i}", minutes=i * 0.5, lat=-23.5 + i * 20 * DEG_PER_METER, lon=-46.6)
        for i in range(5)
    ]
    cluster = only(detect_outages(drops, topology()))
    assert cluster.scope == SCOPE_GEO
    assert cluster.element_id == ""
    assert cluster.element_label == "Cluster geográfico"
    assert cluster.confidence == CONFIDENCE_LOW
    assert cluster.affected_count == 5


def test_pontos_alem_do_raio_nao_formam_cluster_geografico():
    drops = [
        drop(f"geo-{i}", minutes=i * 0.5, lat=-23.5 + i * 500 * DEG_PER_METER, lon=-46.6)
        for i in range(5)
    ]
    assert detect_outages(drops, topology()) == []


def test_login_sem_cto_e_sem_coordenada_e_ignorado():
    drops = [*cto_drops("CTO-A", 5, transmitter="T1"), drop("orfao-1", minutes=1)]
    cluster = only(
        detect_outages(
            drops,
            topology(active_logins_per_cto={"CTO-A": 5}, cto_to_transmitter={"CTO-A": "T1"}),
        )
    )
    assert cluster.scope == SCOPE_CTO
    assert "orfao-1" not in cluster.login_ids
    assert cluster.affected_count == 5


# ---------------------------------------------------------------------------
# Trecho suspeito
# ---------------------------------------------------------------------------


def test_trecho_suspeito_em_escopo_pon():
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 3, pon="P1", transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
                cto_names={"CTO-A": "CX-JARDIM-01", "CTO-B": "CX-JARDIM-02"},
            ),
        )
    )
    assert cluster.suspected_segment_label == "trecho CX-JARDIM-01 → CX-JARDIM-02"


def test_trecho_suspeito_parte_da_cto_mais_proxima_do_pop():
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1", pop="POP1") + cto_drops(
        "CTO-B", 3, pon="P1", transmitter="T1", pop="POP1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
                cto_to_pop={"CTO-A": "POP1", "CTO-B": "POP1"},
                cto_coordinates={
                    "CTO-A": (-23.5 + 900 * DEG_PER_METER, -46.6),
                    "CTO-B": (-23.5 + 200 * DEG_PER_METER, -46.6),
                },
                cto_names={"CTO-A": "CX-01", "CTO-B": "CX-02"},
                pop_coordinates={"POP1": (-23.5, -46.6)},
            ),
        )
    )
    # CX-02 está mais perto do POP: é ela que abre o trecho.
    assert cluster.suspected_segment_label == "trecho CX-02 → CX-01"


def test_sem_duas_ctos_integralmente_fora_nao_ha_trecho_suspeito():
    # CTO-B só perdeu 3 de 4 logins: fecha o escopo, mas não está inteira fora.
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 3, pon="P1", transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 4},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_PON
    assert cluster.suspected_segment_label == ""


# ---------------------------------------------------------------------------
# MRR e determinismo
# ---------------------------------------------------------------------------


def test_mrr_at_risk_soma_os_contratos_afetados():
    drops = [
        drop("a-1", cto="CTO-A", transmitter="T1", mrr="99.90"),
        drop("a-2", minutes=1, cto="CTO-A", transmitter="T1", mrr="149.90"),
        drop("a-3", minutes=2, cto="CTO-A", transmitter="T1", mrr="79.90"),
        drop("a-4", minutes=3, cto="CTO-A", transmitter="T1", mrr="0.00"),
        drop("a-5", minutes=4, cto="CTO-A", transmitter="T1", mrr="200.30"),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(active_logins_per_cto={"CTO-A": 5}, cto_to_transmitter={"CTO-A": "T1"}),
        )
    )
    assert cluster.mrr_at_risk == Decimal("530.00")
    assert isinstance(cluster.mrr_at_risk, Decimal)


def test_login_repetido_conta_uma_vez_so():
    drops = [
        *cto_drops("CTO-A", 5, transmitter="T1"),
        drop("CTO-A-0", minutes=5, cto="CTO-A", transmitter="T1", mrr="100.00"),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(active_logins_per_cto={"CTO-A": 5}, cto_to_transmitter={"CTO-A": "T1"}),
        )
    )
    assert cluster.affected_count == 5
    assert len(set(cluster.login_ids)) == 5


def test_ordenacao_estavel_por_tempo_tamanho_e_elemento():
    topo = topology(
        active_logins_per_cto={"CTO-A": 5, "CTO-B": 6, "CTO-C": 6},
        cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T2", "CTO-C": "T3"},
    )
    drops = (
        cto_drops("CTO-A", 5, transmitter="T1", minutes=60)
        + cto_drops("CTO-B", 6, transmitter="T2")
        + cto_drops("CTO-C", 6, transmitter="T3")
    )
    result = detect_outages(drops, topo)
    assert [(c.element_id, c.affected_count) for c in result] == [
        ("CTO-B", 6),
        ("CTO-C", 6),
        ("CTO-A", 5),
    ]
    # Mesma entrada embaralhada → mesma saída.
    assert detect_outages(list(reversed(drops)), topo) == result


def test_trecho_suspeito_em_escopo_olt():
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 3, pon="P2", transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 3, "CTO-B": 3},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
                cto_names={"CTO-A": "CX-01", "CTO-B": "CX-02"},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.suspected_segment_label == "trecho CX-01 → CX-02"


def test_escopo_olt_com_fracao_baixa_ainda_aponta_o_trecho():
    # Reprodução do evento real das 19:46: a OLT está de pé (2% afetado), o que
    # rompeu é um trecho abaixo dela. Confiança MEDIA + trecho suspeito.
    drops = cto_drops("CTO-A", 3, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 3, pon="P2", transmitter="T1", minutes=1
    )
    active = {"CTO-A": 3, "CTO-B": 3}
    transmitters = {"CTO-A": "T1", "CTO-B": "T1"}
    # Resto da OLT, intacto: 30 caixas de 10 logins cada.
    for i in range(30):
        active[f"CTO-OK-{i}"] = 10
        transmitters[f"CTO-OK-{i}"] = "T1"
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto=active,
                cto_to_transmitter=transmitters,
                cto_names={"CTO-A": "CX-01", "CTO-B": "CX-02"},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.confidence == CONFIDENCE_MEDIUM
    assert cluster.affected_fraction < 0.05
    assert cluster.suspected_segment_label == "trecho CX-01 → CX-02"


def test_trecho_suspeito_em_escopo_pop():
    ctos = ["CTO-A", "CTO-B", "CTO-C", "CTO-D"]
    drops = [
        *cto_drops("CTO-A", 3, pon="P1", transmitter="T1", pop="POP1"),
        *cto_drops("CTO-B", 3, pon="P2", transmitter="T1", pop="POP1", minutes=1),
        *cto_drops("CTO-C", 3, pon="P3", transmitter="T2", pop="POP1", minutes=2),
        *cto_drops("CTO-D", 3, pon="P4", transmitter="T2", pop="POP1", minutes=3),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto=dict.fromkeys(ctos, 3),
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1", "CTO-C": "T2", "CTO-D": "T2"},
                cto_to_pop=dict.fromkeys(ctos, "POP1"),
                cto_names={c: f"CX-0{i + 1}" for i, c in enumerate(ctos)},
            ),
        )
    )
    assert cluster.scope == SCOPE_POP
    assert cluster.suspected_segment_label == "trecho CX-01 → CX-02, CX-03, CX-04"


def test_trecho_suspeito_resume_a_lista_quando_ha_muitas_caixas():
    ctos = [f"CTO-{i}" for i in range(6)]
    drops = [
        d
        for i, cto in enumerate(ctos)
        for d in cto_drops(cto, 3, pon=f"P{i}", transmitter="T1", minutes=i * 0.5)
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto=dict.fromkeys(ctos, 3),
                cto_to_transmitter=dict.fromkeys(ctos, "T1"),
                cto_names={cto: f"CX-{i}" for i, cto in enumerate(ctos)},
            ),
        )
    )
    assert cluster.scope == SCOPE_OLT
    assert cluster.affected_count == 18
    assert cluster.suspected_segment_label == "trecho CX-0 → CX-1, CX-2, CX-3 (+2 caixas)"


def test_cto_isolada_nunca_ganha_trecho_suspeito():
    # Escopo CTO é uma caixa só: não há trecho entre caixas a apontar.
    cluster = only(
        detect_outages(
            cto_drops("CTO-A", 6, transmitter="T1"),
            topology(active_logins_per_cto={"CTO-A": 6}, cto_to_transmitter={"CTO-A": "T1"}),
        )
    )
    assert cluster.scope == SCOPE_CTO
    assert cluster.suspected_segment_label == ""


# ---------------------------------------------------------------------------
# Fração afetada: numerador e denominador da mesma população
# ---------------------------------------------------------------------------
# Em produção a PON 364 saiu com `affected_fraction = 2.25` e a PON 385 com
# 1.29 — fração de 225% e 129%, que não deveriam existir. A causa não era
# escala: o denominador da PON era a soma das caixas que qualificaram no degrau
# de CTO, enquanto o numerador pegava todo login da porta, inclusive o de caixa
# que não qualificou. Dois clusters GEO tinham 114% pelo mesmo motivo, com login
# sem CTO no numerador e nenhuma caixa dele no denominador.


def test_fracao_da_pon_usa_o_denominador_da_porta_e_nao_o_das_caixas():
    # 3 caixas de 4 logins na porta P1; duas caem inteiras (8 logins), a terceira
    # perde 1. O numerador da PON pega os 9; o denominador da porta tem 12.
    drops = [
        *cto_drops("CTO-A", 4, pon="P1", transmitter="T1"),
        *cto_drops("CTO-B", 4, pon="P1", transmitter="T1", minutes=1),
        drop("CTO-C-0", minutes=2, cto="CTO-C", pon="P1", transmitter="T1"),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 4, "CTO-B": 4, "CTO-C": 4},
                active_logins_per_pon={"P1": 12},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1", "CTO-C": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_PON
    assert cluster.affected_count == 9
    # Sem a correção: 9 sobre as caixas qualificadas (8) = 112%.
    assert cluster.affected_fraction == pytest.approx(9 / 12)


def test_login_sem_porta_no_cadastro_entra_no_cluster_mas_nao_na_fracao():
    """Quem entra no evento e quem entra na fração são perguntas diferentes.

    Um login sem PON caiu junto, na mesma caixa, e pertence à massiva. Mas o
    denominador é "os logins desta porta", e ele não está lá — contá-lo foi
    metade dos 225%.
    """
    drops = [
        *cto_drops("CTO-A", 4, pon="P1", transmitter="T1"),
        *cto_drops("CTO-B", 4, pon="P1", transmitter="T1", minutes=1),
        drop("sem-pon", minutes=2, cto="CTO-A", transmitter="T1"),
    ]
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 5, "CTO-B": 4},
                active_logins_per_pon={"P1": 9},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_PON
    assert cluster.affected_count == 9
    assert cluster.affected_fraction == pytest.approx(8 / 9)


def test_sem_cobertura_de_pon_no_cadastro_volta_ao_denominador_antigo():
    """Porta que não aparece no cadastro não vira denominador zero (o que apagaria
    a fração): cai na soma das caixas, que é teto e não medida."""
    drops = cto_drops("CTO-A", 4, pon="P1", transmitter="T1") + cto_drops(
        "CTO-B", 4, pon="P1", transmitter="T1", minutes=1
    )
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 4, "CTO-B": 4},
                cto_to_transmitter={"CTO-A": "T1", "CTO-B": "T1"},
            ),
        )
    )
    assert cluster.scope == SCOPE_PON
    assert cluster.affected_fraction == pytest.approx(1.0)


def test_fracao_nunca_passa_de_cem_por_cento():
    """Login de contrato cancelado cai e entra no numerador sem estar no
    denominador. O resto é teto, não informação: 100% já quer dizer 'todo mundo
    que a gente conhece caiu'."""
    drops = cto_drops("CTO-A", 6, transmitter="T1")
    cluster = only(
        detect_outages(
            drops,
            topology(
                active_logins_per_cto={"CTO-A": 4},
                cto_to_transmitter={"CTO-A": "T1"},
            ),
        )
    )
    assert cluster.affected_fraction == pytest.approx(1.0)


def test_geo_conta_so_quem_tem_caixa_nos_dois_lados_da_divisao():
    """No GEO o denominador são as caixas envolvidas, então o numerador também
    só pode ter quem está numa delas — senão o login sem CTO no snapshot entra
    numa divisão de que ele não faz parte (foi assim que saíram os 114%)."""
    passo = 100 * DEG_PER_METER
    # Seis caixas de 2 logins com 1 caído cada: ninguém qualifica no degrau de
    # CTO (denominador pequeno e 50%), então o que sobra é o agrupamento
    # geográfico. Mais dois logins caídos sem CTO no snapshot.
    drops = [
        drop(f"com-cto-{i}", minutes=i * 0.1, cto=f"CTO-{i}", lat=-23.5 + i * passo, lon=-47.4)
        for i in range(6)
    ] + [
        drop(f"sem-cto-{i}", minutes=0.7 + i * 0.1, lat=-23.5 + (6 + i) * passo, lon=-47.4)
        for i in range(2)
    ]
    cluster = only(
        detect_outages(
            drops, topology(active_logins_per_cto={f"CTO-{i}": 2 for i in range(6)})
        )
    )
    assert cluster.scope == SCOPE_GEO
    # Os 8 caíram e os 8 estão na massiva...
    assert cluster.affected_count == 8
    # ...mas a fração divide 6 caídos com caixa pelos 12 logins dessas caixas.
    # Antes eram os 8 sobre os mesmos 12.
    assert cluster.affected_fraction == pytest.approx(6 / 12)
