"""Score de insatisfação do cliente a partir do texto das conversas — issue #50.

A Velus não usa o Opa! IA (sentimento pago), então derivamos um sinal próprio,
barato e explicável: um **léxico PT-BR de negatividade** aplicado às mensagens
escritas pelo próprio cliente (`direction=CLIENT`), combinado com a **nota
likert baixa** que algumas conversas trazem (`evaluations`).

Por que léxico e não um modelo supervisionado já:
    a ingestão de mensagens é lazy (1 chamada por atendimento — ver #47), então
    a massa de texto rotulado ainda é esparsa demais pra treinar um classificador
    PT-BR confiável. O léxico entrega valor imediato e serve de baseline; o
    modelo supervisionado entra "conforme os labels acumulam" (likert baixo +
    desfecho de churn), sem trocar a interface.

O score por cliente alimenta dois lugares:
    1. feature `dissatisfaction_score` do modelo de churn (`churn_ml`), resolvendo
       a feature de conversa antes deferida por falta de vínculo de cliente;
    2. sinal de regra `DISSATISFACTION` no `churn_risk`, que o expõe no dashboard.

As funções de pontuação são puras (recebem listas (data, texto)/(data, nota) e
uma data de referência) → testáveis isoladamente e usáveis *point-in-time* pelo
pipeline de churn (só conta o que era observável até a data de referência).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

# ── Léxico de negatividade PT-BR (normalizado: minúsculo, sem acento) ─────
# Foco no domínio de ISP/suporte: instabilidade, lentidão, quedas, descaso e
# intenção de cancelamento. Termos compostos (com espaço) também são casados
# por fronteira de palavra na regex abaixo.
NEGATIVE_LEXICON = (
    "pessimo", "pessima", "horrivel", "horroroso", "ruim", "pior", "lixo",
    "porcaria", "palhacada", "absurdo", "absurda", "vergonha", "descaso",
    "lento", "lentidao", "lerdo", "travando", "travou", "travada",
    "oscilando", "oscila", "oscilacao", "instavel", "caiu", "cair", "caindo",
    "demora", "demorando", "demorou", "demorada", "cancelar", "cancelamento",
    "cancela", "reclamar", "reclamacao", "reclamando", "insatisfeito",
    "insatisfeita", "irritado", "irritada", "indignado", "indignada",
    "decepcionado", "decepcionada", "revoltado", "nao funciona",
    "nao presta", "sem internet", "sem sinal", "sem conexao", "sem net",
    "nunca funciona", "ninguem resolve", "estou sem", "to sem",
)

# Cada termo vira uma regex com fronteira de palavra (\b...\b), o que também
# casa termos compostos ("sem internet") sem casar substrings indevidas
# (\bcancela\b não dispara dentro de "cancelamento").
_LEXICON_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(term) + r"\b") for term in NEGATIVE_LEXICON
)

# ── Pesos da combinação léxico × likert ─────────────────────────────────
# A fração de mensagens negativas mede tom; a nota baixa é um rótulo humano
# direto. Somados (capados em 1.0) dão um índice [0, 1] de insatisfação.
NEG_FRACTION_WEIGHT = 0.6
LOW_RATING_WEIGHT = 0.4


def _normalize(text: str) -> str:
    """Minúsculo e sem acento — casa o léxico de forma acento-insensível."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def message_is_negative(text: str) -> bool:
    """True se a mensagem contém ≥ 1 termo do léxico de negatividade."""
    if not text:
        return False
    norm = _normalize(text)
    return any(pat.search(norm) for pat in _LEXICON_PATTERNS)


def _low_rating_score(rating: int) -> float:
    """Mapeia a nota likert baixa em [0, 1]: 1→1.0, 2→0.5, 3→0.0, 4-5→0.0."""
    return max((3 - rating) / 2.0, 0.0)


def compute_dissatisfaction(
    messages: list[tuple[Any, str]],
    ratings: list[tuple[Any, int]],
    r: Any,
) -> float:
    """Índice de insatisfação [0, 1] *point-in-time* até `r`.

    Combina a fração de mensagens do cliente com tom negativo (léxico) com a
    pior nota likert observada. Só considera mensagens enviadas e avaliações
    abertas até `r` — nada posterior à data de referência "vaza" pro score.
    Sem dados (cliente sem conversa avaliada/textual), retorna 0.0.
    """
    texts = [txt for sent, txt in messages if sent is not None and sent <= r]
    rts = [
        rt for opened, rt in ratings if opened is not None and opened <= r and rt
    ]

    neg_fraction = 0.0
    if texts:
        negatives = sum(1 for txt in texts if message_is_negative(txt))
        neg_fraction = negatives / len(texts)

    low_rating = 0.0
    if rts:
        low_rating = max(_low_rating_score(rt) for rt in rts)

    score = NEG_FRACTION_WEIGHT * neg_fraction + LOW_RATING_WEIGHT * low_rating
    return min(score, 1.0)


def load_client_messages(
    organization: Organization,
) -> dict[int, list[tuple[Any, str]]]:
    """Por cliente: (sent_at, texto) das mensagens escritas pelo cliente.

    Só roda dentro de escopo `allow_cross_tenant` (filtro de org explícito).
    """
    from apps.atendimento.infrastructure.models import Mensagem

    out: dict[int, list[tuple[Any, str]]] = defaultdict(list)
    for row in Mensagem.objects.filter(
        organization=organization, direction="CLIENT"
    ).values("atendimento__customer_id", "sent_at", "texto"):
        cid = row["atendimento__customer_id"]
        if cid is None:
            continue
        out[cid].append((row["sent_at"], row["texto"] or ""))
    return out


def load_ratings(
    organization: Organization,
) -> dict[int, list[tuple[Any, int]]]:
    """Por cliente: (opened_at, rating) dos atendimentos com nota likert.

    Só roda dentro de escopo `allow_cross_tenant` (filtro de org explícito).
    """
    from apps.atendimento.infrastructure.models import Atendimento

    out: dict[int, list[tuple[Any, int]]] = defaultdict(list)
    for row in Atendimento.objects.filter(
        organization=organization, rating__isnull=False
    ).values("customer_id", "opened_at", "rating"):
        cid = row["customer_id"]
        if cid is None:
            continue
        out[cid].append((row["opened_at"], row["rating"]))
    return out


@allow_cross_tenant(reason="score de insatisfação por org, fora de request HTTP")
def compute_dissatisfaction_scores(organization: Organization) -> dict[int, float]:
    """Score de insatisfação atual (até agora) por cliente da org.

    Retorna {customer_id → score [0, 1]} apenas para clientes com mensagens ou
    avaliações. Usado pelo sinal de regra de churn pra expor o score; o pipeline
    de ML calcula a mesma métrica *point-in-time* na data de referência própria.
    """
    from django.utils import timezone

    now = timezone.now()
    messages_by_cid = load_client_messages(organization)
    ratings_by_cid = load_ratings(organization)

    cids = set(messages_by_cid) | set(ratings_by_cid)
    return {
        cid: compute_dissatisfaction(
            messages_by_cid.get(cid, ()), ratings_by_cid.get(cid, ()), now
        )
        for cid in cids
    }
