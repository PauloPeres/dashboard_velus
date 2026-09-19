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

from apps.dashboards.massivas import compute_massivas_agora, poll_snapshot

from . import PanelSpec, SlideSpec, register

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
        "timeline": dados["timeline"],
        "mapa": dados["mapa"],
        "mapa_quedas_avulsas": dados["mapa_quedas_avulsas"],
    }


def _tem_massiva(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot.get("massivas"))


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
            SlideSpec(
                key="massivas",
                title="Massivas abertas",
                template="dashboards/panels/slides/_massivas.html",
                seconds=20,
            ),
            # O mapa entra na rotação só quando há evento: sem massiva ele seria
            # um mapa bonito sem pergunta, e slide sem pergunta treina a sala a
            # ignorar a TV.
            SlideSpec(
                key="mapa",
                title="Onde",
                template="dashboards/panels/slides/_mapa.html",
                seconds=20,
                only_when=_tem_massiva,
            ),
            SlideSpec(
                key="ultimas24h",
                title="Últimas horas",
                template="dashboards/panels/slides/_ultimas24h.html",
                seconds=15,
            ),
        ),
    )
)


def janela_timeline() -> timedelta:
    """Janela do slide de últimas horas — a mesma da aba, pelo mesmo motivo."""
    from apps.dashboards.massivas import TIMELINE_HOURS

    return timedelta(hours=TIMELINE_HOURS)
