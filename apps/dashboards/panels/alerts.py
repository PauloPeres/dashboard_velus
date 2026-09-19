"""Severidade e alerta do painel — comum a todos os painéis (P6).

Três níveis, e **só o de cima interrompe** (§4 do plano). A tabela de gatilhos
veio de uma consultoria de NOC e é **ponto de partida a calibrar na sala**, não
medida — por isso todos os limiares são constantes nomeadas aqui, num lugar só.

As regras que fazem o alerta continuar funcionando depois do primeiro mês são
mais importantes que os limiares:

- **supressão por persistência**: nada alarma antes de alguns minutos de evento,
  o que mata flap e reboot de OLT. Mas a massiva aparece **na lista desde o
  primeiro segundo** — suprimir o alarme não é esconder o evento;
- **um evento, um alerta**: massiva que cresce atualiza o card, não dispara de
  novo. Quem decide isso é a tela, que guarda o que já anunciou;
- **manutenção programada não alarma.** Sem isso, toda janela avisada treina a
  equipe a ignorar a tela — que é o oposto do que o painel existe para fazer;
- **reconhecida para de gritar.** Depois que alguém assume, o card vira "em
  tratamento": continua visível, para de interromper.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

NIVEL_INFO = "INFO"
NIVEL_ATENCAO = "ATENCAO"
NIVEL_CRITICO = "CRITICO"

_ORDEM = {NIVEL_INFO: 0, NIVEL_ATENCAO: 1, NIVEL_CRITICO: 2}

# Fração da base de clientes. A calibrar na sala: o que é "grande" depende do
# tamanho da operação, e 1% de uma base de 8 mil é 80 pessoas ao telefone.
FRACAO_ATENCAO = 0.01
FRACAO_CRITICO = 0.05

# Escopos que, por si sós, já são o nível. Uma OLT ou um POP fora é evento de
# infraestrutura, independentemente de quantos clientes o detector já viu.
ESCOPOS_CRITICOS = {"OLT", "POP"}
ESCOPOS_ATENCAO = {"PON"}

# Quanto tempo de evento antes de interromper a sala. Mata flap e reboot de OLT,
# que se resolvem sozinhos antes disto.
SUPRESSAO_MINUTOS = 5

# Quanto tempo o takeover ocupa a tela antes de devolvê-la à rotação. Takeover
# permanente mata o painel: a sala para de ver o resto.
TAKEOVER_SEGUNDOS = 45


def nivel_da_massiva(linha: dict[str, Any], *, base_de_clientes: int) -> str:
    """Severidade de uma massiva: INFO, ATENÇÃO ou CRÍTICO.

    Manutenção programada nunca sobe de INFO — ela é esperada, e alarmar o que
    foi avisado é o caminho mais curto para a equipe parar de olhar a tela.
    """
    if linha.get("is_expected"):
        return NIVEL_INFO

    afetados = linha.get("affected_count") or 0
    fracao_da_base = (afetados / base_de_clientes) if base_de_clientes else 0.0
    escopo = linha.get("scope", "")

    if escopo in ESCOPOS_CRITICOS or fracao_da_base >= FRACAO_CRITICO:
        return NIVEL_CRITICO
    if escopo in ESCOPOS_ATENCAO or fracao_da_base >= FRACAO_ATENCAO:
        return NIVEL_ATENCAO
    return NIVEL_INFO


def deve_interromper(linha: dict[str, Any], *, agora: datetime) -> bool:
    """A massiva merece tomar a tela inteira agora?

    Três recusas, e cada uma existe por um motivo diferente:

    - nível abaixo de crítico **não interrompe**. É o que separa "a sala precisa
      largar o que está fazendo" de "está na lista";
    - evento jovem demais não interrompe (supressão por persistência);
    - evento **já reconhecido** não interrompe: alguém assumiu, e insistir é
      transformar o painel em barulho.
    """
    if linha.get("nivel") != NIVEL_CRITICO:
        return False
    if linha.get("acknowledged_at"):
        return False
    inicio = linha.get("started_at")
    if inicio is None:
        return False
    return (agora - inicio).total_seconds() >= SUPRESSAO_MINUTOS * 60


def classificar(
    linhas: list[dict[str, Any]], *, base_de_clientes: int, agora: datetime
) -> dict[str, Any]:
    """Anota o nível em cada massiva e devolve a que deve tomar a tela.

    A escolhida é a mais grave e, no empate, a que tem mais gente fora — a
    ordem em que a sala deve agir.
    """
    for linha in linhas:
        linha["nivel"] = nivel_da_massiva(linha, base_de_clientes=base_de_clientes)

    candidatas = [linha for linha in linhas if deve_interromper(linha, agora=agora)]
    candidatas.sort(
        key=lambda linha: (-_ORDEM[linha["nivel"]], -(linha.get("ainda_fora") or 0))
    )
    return {
        "takeover": candidatas[0] if candidatas else None,
        "takeover_segundos": TAKEOVER_SEGUNDOS,
        "por_nivel": {
            nivel: sum(1 for linha in linhas if linha["nivel"] == nivel)
            for nivel in (NIVEL_CRITICO, NIVEL_ATENCAO, NIVEL_INFO)
        },
        "supressao_minutos": SUPRESSAO_MINUTOS,
    }
