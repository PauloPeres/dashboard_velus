"""Insights da parede — o que a TV mostra quando não há massiva.

Pedido do operador (21/09/2026): com a rede calma, usar a tela para fazer a
equipe pensar. A ideia é boa e tem uma armadilha: frase de efeito vira papel de
parede em uma semana. Então **todo insight aqui é um número medido**, com a
pergunta que ele levanta — nunca um conselho genérico.

Três regras que definem o que entra:

1. **nada de dinheiro.** MRR, churn e meta mudam a audiência da tela, e a
   operação para de olhar (§7 do plano do painel). O que entra é o que a sala
   pode agir sobre;
2. **número antes da opinião.** "A OLT 1 teve 4 massivas em 9 dias" é insight;
   "precisamos melhorar a rede" é enfeite;
3. **se o dado não existe, o insight não aparece.** Cobertura baixa vira o
   próprio insight ("só 39% das conversas têm etiqueta") em vez de virar um
   número inventado.

O slide sorteia entre os disponíveis a cada carregamento — a TV recarrega a
cada 15 min, então a sala vê vários ao longo do dia sem que nenhum vire
paisagem.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

import structlog

_logger = structlog.get_logger(__name__)

# Quantos dias para trás os insights olham. Curto: a sala reage ao que ainda
# está acontecendo, e "no último trimestre" é assunto de reunião, não de parede.
JANELA_DIAS = 30


def _plural(n: int, singular: str, plural: str) -> str:
    return singular if n == 1 else plural


def _reincidencia(org: Any, desde: datetime) -> dict[str, Any] | None:
    """O elemento que mais repetiu — trecho cronicamente ruim, não azar."""
    from collections import Counter

    from apps.network.infrastructure.models import OutageEvent

    contagem = Counter(
        (scope, ext, label)
        for scope, ext, label in OutageEvent.objects.filter(
            organization=org, started_at__gte=desde
        )
        .exclude(element_external_id="")
        .values_list("scope", "element_external_id", "element_label")
    )
    if not contagem:
        return None
    (_, _, label), vezes = contagem.most_common(1)[0]
    if vezes < 2:
        return None
    return {
        "numero": f"{vezes}",
        "titulo": f"massivas em {label} nos últimos {JANELA_DIAS} dias",
        "pergunta": (
            "Trecho que repete não se resolve com reparo pontual. "
            "O que há de fixo ali — travessia, poste de esquina, emenda antiga?"
        ),
    }


def _causa_confirmada(org: Any, desde: datetime) -> dict[str, Any] | None:
    """Quanto do que aconteceu a gente sabe explicar."""
    from apps.network.infrastructure.models import OutageEvent

    base = OutageEvent.objects.filter(
        organization=org, ended_at__isnull=False, ended_at__gte=desde
    )
    total = base.count()
    if total < 3:
        return None
    com_causa = base.exclude(confirmed_cause="").count()
    pct = round(com_causa * 100 / total)
    return {
        "numero": f"{pct}%",
        "titulo": f"das massivas encerradas têm causa registrada ({com_causa} de {total})",
        "pergunta": (
            "Sem a causa, o sistema segue chutando energia ou fibra e nunca "
            "descobre se acerta. Dois cliques na aba fecham esse ciclo."
        ),
    }


def _tempo_de_reparo(org: Any, desde: datetime) -> dict[str, Any] | None:
    """Quanto tempo a massiva média levou para acabar."""
    from apps.network.infrastructure.models import OutageEvent

    duracoes = [
        (fim - inicio).total_seconds() / 60
        for inicio, fim in OutageEvent.objects.filter(
            organization=org, ended_at__isnull=False, ended_at__gte=desde
        ).values_list("started_at", "ended_at")
    ]
    if len(duracoes) < 3:
        return None
    duracoes.sort()
    mediana = int(duracoes[len(duracoes) // 2])
    pior = int(duracoes[-1])
    return {
        "numero": f"{mediana} min",
        "titulo": f"é a duração típica de uma massiva aqui (pior caso: {pior} min)",
        "pergunta": (
            "A mediana é o caso comum; o pior caso é o que o cliente conta "
            "para o vizinho. O que aconteceu naquele?"
        ),
    }


def _cobertura_da_planta(org: Any) -> dict[str, Any] | None:
    """Quanto da rede o cadastro descreve — limite de tudo que a tela afirma."""
    from apps.network.infrastructure.models import Connection

    total = Connection.objects.filter(organization=org).count()
    if not total:
        return None
    com_pon = Connection.objects.filter(organization=org).exclude(
        pon_external_id=""
    ).count()
    pct = round(com_pon * 100 / total)
    if pct >= 95:
        return None
    return {
        "numero": f"{pct}%",
        "titulo": f"dos logins têm porta PON no cadastro ({com_pon} de {total})",
        "pergunta": (
            "É por esse campo que o sistema sabe quem cai junto. Onde ele "
            "falta, a massiva aparece menor do que é."
        ),
    }


def _caixas_sem_cabo(org: Any) -> dict[str, Any] | None:
    """Caixas que o projeto do InMap não cobre."""
    from apps.dashboards.massivas import tracados_de_cabo
    from apps.network.domain.geometry import RAIO_CANDIDATO_METROS, distance_to_path
    from apps.network.infrastructure.models import NetworkElement

    tracados = tracados_de_cabo(org)
    if not tracados:
        return None
    caixas = [
        (float(lat), float(lon))
        for lat, lon in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            latitude__isnull=False,
            longitude__isnull=False,
        ).values_list("latitude", "longitude")
    ]
    if len(caixas) < 50:
        return None
    sem_cabo = sum(
        1
        for p in caixas
        if min(distance_to_path(p, t.points) for t in tracados) > RAIO_CANDIDATO_METROS
    )
    if not sem_cabo:
        return None
    pct = round(sem_cabo * 100 / len(caixas))
    return {
        "numero": f"{pct}%",
        "titulo": (
            f"das caixas não têm cabo cadastrado por perto "
            f"({sem_cabo} de {len(caixas)})"
        ),
        "pergunta": (
            "Nelas, o sistema não consegue apontar por onde a fibra passa — e "
            "o técnico chega sem essa pista."
        ),
    }


def _fila_de_causa(org: Any) -> dict[str, Any] | None:
    """Massivas esperando alguém dizer o que foram."""
    from apps.network.infrastructure.models import OutageEvent

    pendentes = OutageEvent.objects.filter(
        organization=org,
        ended_at__isnull=False,
        confirmed_cause="",
        cause_waived_at__isnull=True,
    ).count()
    if pendentes < 3:
        return None
    return {
        "numero": f"{pendentes}",
        "titulo": f"{_plural(pendentes, 'massiva espera', 'massivas esperam')} causa",
        "pergunta": (
            "Quem esteve no reparo sabe a resposta; daqui a uma semana, "
            "ninguém mais sabe."
        ),
    }


_FONTES = (
    _reincidencia,
    _causa_confirmada,
    _tempo_de_reparo,
    _fila_de_causa,
    _cobertura_da_planta,
    _caixas_sem_cabo,
)


def coletar(org: Any, now: datetime) -> list[dict[str, Any]]:
    """Os insights disponíveis agora — só os que têm dado para sustentar.

    Cada fonte devolve `None` quando a amostra é pequena demais. Uma parede com
    "1 massiva em 30 dias, será que é um trecho ruim?" ensina a sala a
    desconfiar da tela, e desconfiança não se desfaz com um número bom depois.
    """
    desde = now - timedelta(days=JANELA_DIAS)
    saida: list[dict[str, Any]] = []
    for fonte in _FONTES:
        try:
            # Duas assinaturas: a maioria olha uma janela, duas olham o cadastro
            # inteiro (que não tem "últimos 30 dias").
            dado = fonte(org, desde) if fonte.__code__.co_argcount == 2 else fonte(org)
        except Exception:
            # Um insight quebrado não pode derrubar a parede inteira — mas some
            # em silêncio seria pior que o erro: fica no log.
            _logger.warning("panel_insight_falhou", fonte=fonte.__name__, exc_info=True)
            continue
        if dado:
            saida.append(dado)
    return saida


def sortear(insights: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Um insight por carregamento.

    Sorteio, e não rodízio fixo: a TV recarrega a cada 15 min, e uma ordem
    previsível faria a sala decorar a sequência e parar de ler.
    """
    return random.choice(insights) if insights else None  # noqa: S311
