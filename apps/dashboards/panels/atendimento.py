"""Slide de atendimento do painel de NOC (P10) — a fila do Opa! Suite.

Por que um slide de atendimento numa TV de rede: a fila é o **sintoma** que
aparece antes de a massiva fechar escopo. Trinta pessoas ligando do mesmo bairro
é evento de rede antes de ser evento de atendimento, e a sala vê isso primeiro
aqui.

Três números, e só três (§2 do plano — tudo que não serve a "está tudo bem? se
não, onde?" é enfeite):

1. **quantos esperam agora** — a fila;
2. **há quanto tempo espera o mais antigo** — é ele que vira reclamação;
3. **o volume da hora contra o normal daquela hora** — "4,1× o normal" é sinal
   de massiva; "dentro do normal" é uma terça-feira.

O terceiro reaproveita o baseline sazonal que a aba de Tendências já usa
(`atendimento_hora_esperado`). Recalcular "o normal" aqui, com outra conta,
faria a TV e a aba discordarem — e no dia da discordância ninguém saberia qual
das duas acreditar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# Acima disto o cliente mais antigo da fila já é uma reclamação. Ponto de
# partida a calibrar na sala, como o resto dos limiares do painel.
ESPERA_ALERTA_MINUTOS = 30


def snapshot_atendimento(org: Any, now: datetime) -> dict[str, Any]:
    """Fila, espera mais longa e o volume da hora contra o normal.

    Devolve `disponivel=False` quando a organização não tem atendimento
    sincronizado — e aí o slide some da rotação em vez de mostrar três zeros,
    que numa TV se leem como "está tudo calmo".
    """
    from apps.atendimento.infrastructure.models import Atendimento

    abertos = list(
        Atendimento.objects.filter(
            organization=org,
            status__in=[Atendimento.Status.OPEN, Atendimento.Status.IN_PROGRESS],
        )
        .only("opened_at", "departamento_external_id", "customer_name")
        .order_by("opened_at")[:200]
    )
    if not Atendimento.objects.filter(organization=org).exists():
        return {"disponivel": False}

    mais_antigo = next((a for a in abertos if a.opened_at), None)
    espera_min = (
        int((now - mais_antigo.opened_at).total_seconds() // 60) if mais_antigo else 0
    )

    hora = now.replace(minute=0, second=0, microsecond=0)
    volume = _volume_da_hora(org, hora)

    return {
        "disponivel": True,
        "na_fila": len(abertos),
        "espera_minutos": espera_min,
        "espera_alerta": espera_min >= ESPERA_ALERTA_MINUTOS,
        "volume": volume,
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


def janela_de_fila() -> timedelta:
    """Quanto tempo para trás a fila é considerada 'agora'."""
    return timedelta(hours=24)
