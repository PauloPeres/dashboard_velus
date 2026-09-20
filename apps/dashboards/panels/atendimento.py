"""Slide de atendimento do painel de NOC (P10) — a fila do Opa! Suite.

Por que um slide de atendimento numa TV de rede: a fila é o **sintoma** que
aparece antes de a massiva fechar escopo. Trinta pessoas ligando do mesmo bairro
é evento de rede antes de ser evento de atendimento, e a sala vê isso primeiro
aqui.

**O que a primeira versão errou, e a verificação em produção pegou.** Contar todo
atendimento com status aberto dava *826 na fila, com 122 dias de espera*. Medido
em 19/09/2026: são **813 conversas em "em atendimento"** que nunca foram fechadas
na origem, a mais antiga de 20/05. Isso não é fila — é resíduo de cadastro, e
numa TV viraria um número gigante que a sala aprenderia a ignorar em uma semana.

Daí as três separações desta versão:

1. **fila** é o que entrou na janela recente (24 h) e ainda está aberto;
2. **parados** são os abertos mais antigos que isso, contados à parte e
   nomeados pelo que são;
3. **frescor**: a fila só quer dizer alguma coisa se o atendimento estiver
   sincronizado. *Medido no mesmo dia:* o último sync do Opa tinha rodado 1,5 dia
   antes, e "0 na fila" ali significava "não estamos enxergando", não "está
   calmo" — a diferença que o painel inteiro existe para não apagar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# Janela do que é "fila agora". Conversa aberta há mais de um dia não é alguém
# esperando na linha: é conversa que ninguém fechou.
JANELA_FILA_HORAS = 24

# Acima disto o cliente mais antigo da fila já é uma reclamação. Ponto de
# partida a calibrar na sala, como o resto dos limiares do painel.
ESPERA_ALERTA_MINUTOS = 30

# Sem conversa nova por mais tempo que isto, o número da fila deixa de falar
# sobre a operação e passa a falar sobre a coleta.
FRESCOR_ALERTA_HORAS = 6


def snapshot_atendimento(org: Any, now: datetime) -> dict[str, Any]:
    """Fila recente, parados, espera mais longa e o volume da hora.

    Devolve `disponivel=False` quando a organização não tem atendimento
    sincronizado — e aí o slide some da rotação em vez de mostrar três zeros,
    que numa TV se leem como "está tudo calmo".
    """
    from apps.atendimento.infrastructure.models import Atendimento

    base = Atendimento.objects.filter(organization=org)
    if not base.exists():
        return {"disponivel": False}

    em_aberto = base.filter(
        status__in=[Atendimento.Status.OPEN, Atendimento.Status.IN_PROGRESS]
    )
    corte = now - timedelta(hours=JANELA_FILA_HORAS)

    fila = list(
        em_aberto.filter(opened_at__gte=corte).only("opened_at").order_by("opened_at")
    )
    parados = em_aberto.filter(opened_at__lt=corte).count()

    mais_antigo = fila[0] if fila else None
    espera_min = (
        int((now - mais_antigo.opened_at).total_seconds() // 60) if mais_antigo else 0
    )

    # Frescor do próprio atendimento: a conversa mais recente que existe no
    # banco. É o que separa "ninguém ligou" de "paramos de enxergar".
    ultima = (
        base.exclude(opened_at=None)
        .order_by("-opened_at")
        .values_list("opened_at", flat=True)
        .first()
    )
    idade_horas = int((now - ultima).total_seconds() // 3600) if ultima else None

    return {
        "disponivel": True,
        "na_fila": len(fila),
        "janela_horas": JANELA_FILA_HORAS,
        # Contados à parte e nomeados pelo que são: conversas que ninguém fechou
        # na origem. Somá-las à fila daria 826 numa TV — número que a sala
        # aprende a ignorar em uma semana.
        "parados": parados,
        "espera_minutos": espera_min,
        "espera_alerta": espera_min >= ESPERA_ALERTA_MINUTOS,
        "ultima_conversa": ultima,
        "idade_horas": idade_horas,
        "dado_velho": idade_horas is None or idade_horas >= FRESCOR_ALERTA_HORAS,
        "volume": _volume_da_hora(org, now.replace(minute=0, second=0, microsecond=0)),
    }


def _volume_da_hora(org: Any, hora: datetime) -> dict[str, Any]:
    """Real vs esperado da hora corrente, pelo baseline sazonal da aba.

    A hora corrente está **incompleta** — às 14h10 ela tem dez minutos de
    volume contra uma hora de esperado. Por isso a comparação vem marcada como
    parcial: sem isso, toda hora começaria "abaixo do normal" e o número não
    diria nada até as 14h59.
    """
    from apps.analytics.application.aggregations import atendimento_hora_esperado

    try:
        dados = atendimento_hora_esperado(org, hour_start=hora, foco="todos")
    except Exception:  # o painel nunca cai por causa de um slide
        return {"medivel": False}

    return {
        "medivel": True,
        "real": dados.get("real", 0),
        "esperado": round(dados.get("esperado", 0.0), 1),
        "ratio": dados.get("ratio"),
        "anomalia": bool(dados.get("anomalia")),
        "parcial": True,
        "hora": hora,
    }


def tem_atendimento(snapshot: dict[str, Any]) -> bool:
    """O slide só entra na rotação onde existe atendimento sincronizado."""
    return bool(snapshot.get("atendimento", {}).get("disponivel"))
