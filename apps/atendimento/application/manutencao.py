"""Janelas de manutenção programada — o serviço que os outros contextos usam.

O cadastro de manutenção mora em `EventoRede` (aba de Tendências), onde a equipe
já anota o que mexeu na rede. A aba de Massivas precisa da mesma informação para
não alarmar o que foi avisado — e a decisão (19/09/2026) foi **um cadastro só**:
dois cadastros paralelos garantiriam o dia em que alguém avisa num lugar e a
massiva alarma do outro.

Este módulo existe porque `apps.network` não importa models de `apps.atendimento`
(AGENT.md §1.1). Ele pergunta aqui, e recebe um objeto simples — sem ORM
atravessando a fronteira.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apps.atendimento.infrastructure.models import EventoRede


@dataclass(frozen=True)
class JanelaDeManutencao:
    """Uma manutenção programada, do jeito que o outro contexto precisa ver."""

    id: int
    titulo: str
    scope: str
    element_external_id: str
    starts_at: datetime
    ends_at: datetime | None

    def cobre(self, *, scope: str, element_external_id: str, moment: datetime) -> bool:
        """A janela explica um evento deste escopo, neste instante?

        Três recusas deliberadas:

        - **evento pontual não cobre nada.** `ended_at` vazio significa
          "aconteceu às 14h", não "começou às 14h e não sei quando acaba". Tratar
          o segundo caso como janela aberta marcaria como esperada toda massiva
          posterior, para sempre;
        - **escopo diferente não cobre.** Manutenção na OLT 2 não explica queda
          na OLT 1;
        - **não se sobe a hierarquia.** Uma janela de OLT não cobre
          automaticamente as CTOs abaixo dela. Pareceria esperto e marcaria como
          esperada uma caixa que nada tem a ver com o que foi anunciado — e o
          preço do erro aqui é a equipe não atender um rompimento de verdade.
        """
        if self.ends_at is None:
            return False
        if not (self.starts_at <= moment <= self.ends_at):
            return False
        if not self.scope:
            return True
        if self.scope != scope:
            return False
        if not self.element_external_id:
            return True
        return self.element_external_id == element_external_id


def janela_que_cobre(
    organization: Any,
    *,
    scope: str,
    element_external_id: str,
    moment: datetime,
) -> JanelaDeManutencao | None:
    """A manutenção programada que explica um evento — ou None.

    Consulta estreita de propósito: só janelas que já começaram e ainda não
    terminaram no instante perguntado. A lista de manutenções é curta (dezenas
    por ano), então o filtro fino fica em `cobre`, onde está escrito por quê.
    """
    candidatas = EventoRede.objects.filter(
        organization=organization,
        tipo=EventoRede.Tipo.MANUTENCAO,
        started_at__lte=moment,
        ended_at__gte=moment,
    ).only("id", "titulo", "scope", "element_external_id", "started_at", "ended_at")

    for evento in candidatas:
        janela = JanelaDeManutencao(
            id=evento.pk,
            titulo=evento.titulo,
            scope=evento.scope,
            element_external_id=evento.element_external_id,
            starts_at=evento.started_at,
            ends_at=evento.ended_at,
        )
        if janela.cobre(
            scope=scope, element_external_id=element_external_id, moment=moment
        ):
            return janela
    return None
