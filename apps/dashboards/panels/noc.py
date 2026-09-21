"""Painel de NOC — a TV da sala de operação.

A pergunta que ele responde, **a 4 metros e em 2 segundos**: *está tudo bem? se
não, onde?* Tudo que não serve a isso é enfeite e fica de fora — MRR, churn e
meta não entram, porque mudam a audiência da tela e a operação para de olhar
(§7 do `docs/massivas-painel-tv-plano.md`).

O snapshot é **uma consulta só por volta da rotação**, não uma por slide: a TV
fica ligada meses e não pode virar carga no banco nem no IXC.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from apps.dashboards.massivas import (
    base_de_clientes,
    compute_mapa_do_dia,
    compute_massivas_agora,
    poll_snapshot,
)

from . import PanelSpec, SlideSpec, register
from .alerts import classificar
from .atendimento import snapshot_atendimento, tem_atendimento
from .insights import coletar as coletar_insights
from .insights import sortear as sortear_insight

# Acima disto o dado é velho o bastante para a tela avisar em vez de deixar a
# sala supor que está tudo calmo. É o mesmo limite do poll (§7 do plano de
# massivas), porque é o mesmo dado.
FRESCOR_ALERTA_SEGUNDOS = 10 * 60


def _snapshot(org: Any, now: datetime) -> dict[str, Any]:
    """Tudo que os slides do NOC precisam, numa estrutura só.

    Inclui o **estado da coleta** e a idade do dado em segundos. Não é enfeite:
    é o que permite à tela declarar que está velha em vez de mostrar números
    antigos com cara de agora — a regra inegociável do painel (§3 do plano).
    """
    dados = compute_massivas_agora(org, now=now)
    poll = poll_snapshot(org, now=now)

    ultimo_sucesso = poll.get("last_success_at")
    idade_s = (
        int((now - ultimo_sucesso).total_seconds()) if ultimo_sucesso else None
    )

    abertas = dados["linhas"]
    maior = dados.get("maior_massiva")
    # O mapa DE CADA massiva (T5): na parede, uma página por evento, com o
    # resumo de um lado e o mapa dele do outro. O mapa geral respondia "onde
    # estão as massivas"; com três abertas, ninguém sabia qual ponto era de
    # qual — que é justamente a pergunta na hora de agir.
    _anexa_mapa_por_massiva(org, abertas, dados)
    # Severidade e takeover (P6). A classificação anota o nível em cada linha,
    # então tem que rodar antes de a tela desenhar a lista.
    base = base_de_clientes(org)
    alerta = classificar(abertas, base_de_clientes=base, agora=now)
    return {
        "gerado_em": now,
        # A barra fixa, visível em todos os slides.
        "barra": {
            "clientes_fora": dados["clientes_fora"],
            "massivas_abertas": dados["massivas_abertas"],
            "coleta_ok": bool(poll.get("ok")),
            "coleta_mensagem": poll.get("mensagem", ""),
            "idade_segundos": idade_s,
            # O painel se recusa a mostrar número quando não sabe a idade do
            # dado: "—" é resposta, zero seria mentira.
            "idade_conhecida": idade_s is not None,
            "dado_velho": idade_s is None or idade_s > FRESCOR_ALERTA_SEGUNDOS,
        },
        "semaforo": {
            "tudo_bem": dados["massivas_abertas"] == 0 and bool(poll.get("ok")),
            "maior": maior,
            "clientes_que_voltaram": dados["clientes_que_voltaram"],
            "janela_retorno_horas": dados["janela_retorno_horas"],
        },
        # Ordenadas por impacto: quem tem mais gente fora primeiro. É a ordem em
        # que a sala deve agir.
        "massivas": sorted(abertas, key=lambda linha: -linha["ainda_fora"]),
        "alerta": alerta,
        "base_de_clientes": base,
        # A fila do atendimento (P10): é o sintoma que aparece antes de a
        # massiva fechar escopo.
        "atendimento": snapshot_atendimento(org, now),
        # O que a parede mostra quando a rede está calma: um número medido da
        # operação e a pergunta que ele levanta. Sorteado a cada carregamento.
        "insight": sortear_insight(coletar_insights(org, now)),
        # O mapa do dia (21/09/2026): a TV só tinha mapa dentro da página de uma
        # massiva aberta, então com a rede calma não havia mapa nenhum. Este
        # sempre tem o que mostrar — é onde a rede doeu nas últimas 24 h.
        "mapa_dia": compute_mapa_do_dia(org, now=now),
        "timeline": dados["timeline"],
        "mapa": dados["mapa"],
        "mapa_quedas_avulsas": dados["mapa_quedas_avulsas"],
    }


def _anexa_mapa_por_massiva(
    org: Any, linhas: list[dict[str, Any]], dados: dict[str, Any]
) -> None:
    """Põe em cada linha o recorte de mapa da sua própria massiva.

    Reaproveita `compute_mapa`, o mesmo da aba — o mapa da TV não pode ser um
    segundo desenho com regras próprias, ou as duas telas vão discordar sobre
    onde está o problema.
    """
    from apps.dashboards.massivas import compute_cabos_candidatos, compute_mapa

    quedas_por_massiva = dados.get("_quedas_por_massiva") or {}
    for linha in linhas:
        quedas = quedas_por_massiva.get(linha["id"], [])
        cabos = (linha.get("cabos") or {}).get("ids_no_mapa") or []
        if not cabos:
            cabos = (compute_cabos_candidatos(org, quedas) or {}).get("ids_no_mapa", [])
        linha["mapa"] = compute_mapa(
            org,
            quedas,
            vizinhas_intactas=(linha.get("vizinhanca") or {}).get("intactas", []),
            cabos_candidatos=cabos,
        )


def _tem_massiva(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot.get("massivas"))


def _sem_massiva_e_com_insight(snapshot: dict[str, Any]) -> bool:
    """O insight ocupa a parede só quando não há evento.

    Com massiva aberta, a tela tem assunto — e disputar espaço com ele seria
    trocar o urgente pelo interessante.
    """
    return not snapshot.get("massivas") and bool(snapshot.get("insight"))


def _tem_mapa_do_dia(snapshot: dict[str, Any]) -> bool:
    """Sem caixa no mapa, o slide sai da rotação (T4).

    Acontece em base pequena ou em dia realmente calmo — e um mapa vazio numa
    parede ensina a sala a ignorar a TV.
    """
    return bool((snapshot.get("mapa_dia") or {}).get("pontos"))


def _tem_timeline(snapshot: dict[str, Any]) -> bool:
    """A linha do tempo só entra com barra para mostrar (T4).

    Slide vazio numa parede ensina a sala a ignorar a TV — e a linha do tempo
    passa a maior parte do dia zerada, que é o estado normal da rede.
    """
    return any(b.get("fora") or b.get("voltaram") for b in snapshot.get("timeline") or [])


PANEL = register(
    PanelSpec(
        key="noc",
        title="NOC",
        subtitle="Quedas e massivas em tempo real",
        snapshot=_snapshot,
        access_key="massivas",
        slides=(
            # O slide casa. Com a rede saudável ele fica calmo e parado —
            # silêncio é informação, e é o que faz a sala confiar no vermelho
            # quando ele aparece.
            SlideSpec(key="semaforo", title="Situação", template="dashboards/panels/slides/_semaforo.html", seconds=15),
            # UMA PÁGINA POR MASSIVA (T5): resumo à esquerda, mapa daquela
            # massiva à direita. Substituiu a lista de massivas + o mapa geral,
            # que juntos respondiam "quais existem" e "onde estão", mas nunca
            # "onde está ESTA".
            SlideSpec(
                key="massiva",
                title="Massiva aberta",
                template="dashboards/panels/slides/_massiva_pagina.html",
                seconds=20,
                repeat_key="massivas",
            ),
            # Rede calma: em vez de tela parada, um número da operação e a
            # pergunta que ele levanta (pedido de 21/09/2026).
            SlideSpec(
                key="insight",
                title="Para pensar",
                template="dashboards/panels/slides/_insight.html",
                seconds=18,
                only_when=_sem_massiva_e_com_insight,
            ),
            # Onde a rede doeu nas últimas 24 h, por caixa. Pedido do operador
            # em 21/09/2026, depois de notar que com a rede calma a TV não
            # mostrava mapa nenhum.
            SlideSpec(
                key="mapa_dia",
                title="Onde a rede doeu hoje",
                template="dashboards/panels/slides/_mapa_dia.html",
                seconds=20,
                only_when=_tem_mapa_do_dia,
            ),
            SlideSpec(
                key="ultimas24h",
                title="Últimas horas",
                template="dashboards/panels/slides/_ultimas24h.html",
                seconds=15,
                only_when=_tem_timeline,
            ),
            # Só entra onde existe atendimento sincronizado: três zeros numa TV
            # se leem como "está tudo calmo", que é o oposto de "não sei".
            SlideSpec(
                key="atendimento",
                title="Atendimento",
                template="dashboards/panels/slides/_atendimento.html",
                seconds=15,
                only_when=tem_atendimento,
            ),
        ),
    )
)


def janela_timeline() -> timedelta:
    """Janela do slide de últimas horas — a mesma da aba, pelo mesmo motivo."""
    from apps.dashboards.massivas import TIMELINE_HOURS

    return timedelta(hours=TIMELINE_HOURS)
