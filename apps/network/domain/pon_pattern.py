"""O padrão da massiva lido por PON — corte de tronco, PON inteira, OLT inteira.

Achado da massiva 359 (30/09/2026): das PONs da OLT 1, 42 caíram de 80% a 100%
e todas as outras ficaram em 0%. Nenhuma ficou no meio. Esse "binário por PON"
é a assinatura física de um cabo que leva várias PONs rompido **antes** dos
splitters: a PON é uma fibra, e fibra cortada derruba a PON inteira; a PON cuja
fibra não passa pelo corte não sente nada.

O padrão diz o **tipo** do problema antes de a rota dizer o lugar — e diz isso
com dado que não depende do desenho do projeto: só de quem caiu e de quem está
online em cada PON.

Domínio puro: entram contagens por PON, sai a classificação.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Uma PON "caiu inteira" com 70% ou mais dos clientes fora. Não 100%: sempre há
# o cliente que voltou antes, o que tinha ONU com outra rota, o cadastro velho.
# Na 359, as 42 PONs caídas ficaram entre 81% e 100%.
FRACAO_INTEIRA = 0.7

# Até 15% fora é ruído da PON — o cliente que desligou o roteador na mesma hora.
# Na 359 a PON 223 teve 2 de 21 fora e não tinha nada a ver com o corte.
FRACAO_RESIDUAL = 0.15

# PON com menos clientes que isto não tem o que dizer: 1 de 1 fora é 100% e não
# significa nada.
CLIENTES_MINIMOS = 2

TRONCO = "TRONCO"
OLT_INTEIRA = "OLT"
PON_INTEIRA = "PON"


@dataclass(frozen=True)
class PadraoPon:
    """Como as PONs das OLTs envolvidas caíram."""

    tipo: str
    pons_inteiras: tuple[str, ...]
    pons_parciais: tuple[str, ...]
    pons_intactas: int

    @property
    def tem_padrao(self) -> bool:
        return bool(self.tipo)


def classificar(fora: Mapping[str, int], no_ar: Mapping[str, int]) -> PadraoPon:
    """Classifica pela forma como as PONs caíram.

    `fora` é quantos clientes da massiva caíram em cada PON; `no_ar` é quantos
    clientes de cada PON das mesmas OLTs estão online agora, **sem contar** os
    da massiva (quem já voltou continua sendo "fora" para a massiva — contá-lo
    nos dois lados diluiria a PON pela metade depois do reparo).

    - **tronco**: duas ou mais PONs inteiras, quase nenhuma parcial, e PONs da
      mesma OLT intactas — um cabo com várias fibras de PON, cortado antes dos
      splitters;
    - **OLT inteira**: todas as PONs com cliente caíram — antes de procurar
      cabo, olhe equipamento e energia do POP;
    - **PON inteira**: uma PON só, inteira — a fibra dela antes do primeiro
      splitter, ou a porta da OLT;
    - vazio: qualquer outra forma (quedas parciais espalhadas são ramal depois
      do splitter, energia de bairro, ou mais de um evento). A tela não rotula o
      que não sabe.
    """
    inteiras: list[str] = []
    parciais: list[str] = []
    intactas = 0
    for pon in set(fora) | set(no_ar):
        caidos = fora.get(pon, 0)
        total = caidos + no_ar.get(pon, 0)
        if total < CLIENTES_MINIMOS:
            continue
        fracao = caidos / total
        if fracao >= FRACAO_INTEIRA:
            inteiras.append(pon)
        elif fracao > FRACAO_RESIDUAL:
            parciais.append(pon)
        else:
            # Residual conta como intacta: a PON está no ar, e o cliente que caiu
            # nela caiu por outro motivo.
            intactas += 1

    tipo = ""
    if len(inteiras) >= 2 and len(parciais) <= max(1, len(inteiras) // 5):
        tipo = OLT_INTEIRA if not intactas and not parciais else TRONCO
    elif len(inteiras) == 1 and len(parciais) <= 1:
        tipo = PON_INTEIRA

    return PadraoPon(
        tipo=tipo,
        pons_inteiras=tuple(sorted(inteiras, key=_ordem_de_pon)),
        pons_parciais=tuple(sorted(parciais, key=_ordem_de_pon)),
        pons_intactas=intactas,
    )


def _ordem_de_pon(pon: str) -> tuple[int, str]:
    """Numérica quando der: "PON 9" antes de "PON 10"."""
    return (int(pon), pon) if pon.isdigit() else (1 << 30, pon)
