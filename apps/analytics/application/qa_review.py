"""IA supervisora de QA — LLM-as-judge sobre as conversas de atendimento.

Pega uma conversa fechada, **redige a PII** (LGPD — ver `redaction`) e a envia
ao Claude com uma rubrica PT-BR pedindo um JSON estruturado: resolveu? tom,
empatia, aderência ao script, SLA, categoria do motivo, resumo e o que poderia
melhorar, mais um score 0–100. O resultado vira uma linha `QAReview` (1 por
atendimento) que alimenta o scorecard de QA do dashboard.

Por que LLM-as-judge e não um classificador local: o Claude já entende PT-BR e
a rubrica, sem peso no cluster k3s; treinar um modelo caseiro exigiria muito
texto rotulado (a ingestão de mensagens é lazy/esparsa). Custo controla-se por
amostragem (ver `select_conversations_for_qa`) e modelo barato (Haiku default).

Puramente analítico: avalia a conversa, nunca responde ao cliente.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from django.conf import settings
from django.utils import timezone

from apps.analytics.infrastructure.models import QAReview
from apps.atendimento.application.messages import get_or_fetch_messages
from apps.atendimento.application.redaction import redact
from apps.atendimento.domain.bots import is_bot_atendente
from apps.atendimento.infrastructure.models import Atendimento, Mensagem
from apps.shared.decorators import allow_cross_tenant
from apps.tenancy.models import Organization

_logger = structlog.get_logger(__name__)

# Rubrica do juiz. Exige JSON estrito (sem prosa em volta) — `_parse_review`
# ainda é tolerante, mas a instrução reduz ruído.
QA_SYSTEM_PROMPT = """\
Você é um supervisor de qualidade de atendimento de um provedor de internet \
(ISP). Avalie a conversa abaixo de forma objetiva e em português do Brasil.

Considere:
- resolveu: o problema/pedido do cliente foi efetivamente resolvido na conversa?
- sla_ok: o atendente respondeu em tempo razoável, sem deixar o cliente esperando?
- tom: cordialidade e profissionalismo do atendente (1 ruim a 5 excelente).
- empatia: o atendente demonstrou entender e se importar com o cliente (1 a 5).
- aderencia: seguiu boas práticas (saudação, identificação do problema, \
confirmação da solução, encerramento) (1 a 5).
- categoria: motivo principal em poucas palavras (ex.: "sem conexão", \
"financeiro/2ª via", "lentidão", "cancelamento", "agendamento de visita").
- resumo: 1 frase do que aconteceu.
- melhoria: 1 frase com a principal oportunidade de melhoria (ou "nenhuma").
- overall_score: nota geral de qualidade de 0 a 100.

Os dados pessoais foram mascarados ([NOME], [CPF], [TELEFONE], ...) — ignore os \
marcadores ao avaliar.

Responda APENAS com um objeto JSON válido, sem texto antes ou depois, no formato:
{"resolveu": true, "sla_ok": true, "tom": 4, "empatia": 4, "aderencia": 3, \
"categoria": "sem conexão", "resumo": "...", "melhoria": "...", \
"overall_score": 78}
"""

# Rubrica do juiz para BOT de autoatendimento (Gi/Felipe). De um bot NÃO se
# espera empatia/cordialidade humana — espera-se resolver sozinho (deflexão) ou,
# quando não der, ESCALAR rápido pro humano sem prender o cliente em loop.
# Avaliar bot com a rubrica humana o pune injustamente (era por isso que os bots
# apareciam como "piores atendentes"). As dimensões do bot reusam as colunas
# likert do QAReview (ver `_BOT_TO_COLUMNS`); o JSON nomeado fica em `raw`.
QA_BOT_SYSTEM_PROMPT = """\
Você é um supervisor de qualidade avaliando um BOT de autoatendimento de um \
provedor de internet (ISP) no WhatsApp. NÃO avalie cordialidade ou empatia \
humana: de um bot esperamos RESOLVER sozinho (deflexão) ou, quando não \
conseguir, ESCALAR rápido para um atendente humano sem deixar o cliente preso \
em loop. Avalie de forma objetiva e em português do Brasil.

Considere:
- deflexao: o bot resolveu o pedido do cliente SEM precisar de um humano?
- escalou_corretamente: quando NÃO resolveu, encaminhou para um humano de forma \
limpa (sem loop, sem abandonar o cliente)? Se o bot resolveu sozinho, marque true.
- compreensao: entendeu a intenção do cliente, sem forçar menu errado nem \
repetir a mesma pergunta (1 péssimo a 5 perfeito).
- clareza: respostas objetivas e acionáveis, sem muro de texto robótico (1 a 5).
- atrito: o cliente ficou frustrado (pediu humano várias vezes, xingou, \
desistiu)? Use 5 = nenhum atrito, 1 = muito atrito.
- categoria: motivo principal em poucas palavras (ex.: "2ª via boleto", \
"desbloqueio", "consulta de plano", "fora do escopo do bot").
- resumo: 1 frase do que aconteceu.
- melhoria: 1 frase com a principal oportunidade de melhoria do fluxo (ou "nenhuma").
- overall_score: nota geral do desempenho do bot de 0 a 100. O PIOR caso é NÃO \
resolver E NÃO escalar (cliente abandonado).

Os dados pessoais foram mascarados ([NOME], [CPF], [TELEFONE], ...) — ignore os \
marcadores ao avaliar.

Responda APENAS com um objeto JSON válido, sem texto antes ou depois, no formato:
{"deflexao": true, "escalou_corretamente": true, "compreensao": 4, \
"clareza": 4, "atrito": 5, "categoria": "2ª via boleto", "resumo": "...", \
"melhoria": "...", "overall_score": 82}
"""

# Mapeia as chaves da rubrica do bot -> colunas do QAReview (reuso de schema, sem
# migração). O scorecard mostra os rótulos certos por coorte (bot vs humano).
_BOT_TO_COLUMNS = {
    "deflexao": "resolveu",
    "escalou_corretamente": "sla_ok",
    "clareza": "tom",
    "compreensao": "empatia",
    "atrito": "aderencia",
}


def _bot_data_to_columns(data: dict[str, Any]) -> dict[str, Any]:
    """Traduz o JSON da rubrica do bot para as chaves de coluna que `_persist` lê.

    Campos compartilhados (categoria/resumo/melhoria/overall_score) passam
    direto; as dimensões do bot caem nas colunas likert reusadas.
    """
    cols = {
        k: data[bot_key]
        for bot_key, k in _BOT_TO_COLUMNS.items()
        if bot_key in data
    }
    for shared in ("categoria", "resumo", "melhoria", "overall_score"):
        if shared in data:
            cols[shared] = data[shared]
    return cols


_DIRECTION_LABELS = {
    Mensagem.Direction.CLIENT: "[CLIENTE]",
    Mensagem.Direction.AGENT: "[ATENDENTE]",
    Mensagem.Direction.SYSTEM: "[SISTEMA]",
}


def build_transcript(
    atendimento: Atendimento, messages: list[Mensagem]
) -> str:
    """Monta a transcrição **redigida** (PII mascarada) pro juiz.

    Inclui um cabeçalho de contexto (departamento, canal, nota do cliente) e as
    mensagens rotuladas por direção. O nome do atendente não vai pro LLM — fica
    só no snapshot local do `QAReview`.
    """
    names = tuple(n for n in (atendimento.customer_name,) if n)
    documents = tuple(d for d in (atendimento.customer_document,) if d)

    header_parts = [
        f"Departamento: {atendimento.departamento.nome}"
        if atendimento.departamento_id
        else "Departamento: -",
        f"Canal: {atendimento.canal or '-'}",
    ]
    if atendimento.rating is not None:
        header_parts.append(f"Nota do cliente: {atendimento.rating}/5")
    header = " | ".join(header_parts)

    lines = [header, "---"]
    for m in messages:
        label = _DIRECTION_LABELS.get(m.direction, "[OUTRO]")
        texto = redact(m.texto or "", names=names, documents=documents)
        lines.append(f"{label} {texto}".rstrip())
    return "\n".join(lines)


def _coerce_int(value: Any, *, lo: int, hi: int) -> int | None:
    try:
        n = round(float(value))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _parse_review(text: str) -> dict[str, Any]:
    """Extrai o objeto JSON da resposta do juiz, tolerante a cercas/prosa.

    Pega do primeiro `{` ao último `}`. Levanta ValueError se não houver JSON
    parseável — o chamador loga e segue (a conversa não é avaliada desta vez).
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("resposta do juiz sem objeto JSON")
    return json.loads(text[start : end + 1])


@allow_cross_tenant(reason="QA de atendimento roda em Celery, escopo é a org passada")
def review_conversation(
    organization: Organization,
    atendimento: Atendimento,
    *,
    client: Any = None,
    model: str | None = None,
) -> QAReview | None:
    """Avalia um atendimento com o juiz LLM e persiste o `QAReview` (upsert).

    `client` é injetável (fake nos testes) — qualquer objeto com `.judge(...)`.
    Sem client, constrói o `GeminiClient` a partir das settings, mas só se
    `QA_LLM_ENABLED`. Retorna None quando não há mensagens, o QA está desligado
    ou a resposta do juiz não pôde ser parseada (tolerante a falha).
    """
    messages = get_or_fetch_messages(organization, atendimento)
    if not messages:
        return None

    transcript = build_transcript(atendimento, messages)
    model = model or settings.QA_LLM_MODEL
    # Bot de autoatendimento usa rubrica própria (deflexão/escalonamento) em vez
    # da humana (empatia/tom). Identidade vem da fonte única no domínio.
    is_bot = is_bot_atendente(atendimento.atendente_nome)
    system_prompt = QA_BOT_SYSTEM_PROMPT if is_bot else QA_SYSTEM_PROMPT
    log = _logger.bind(
        organization=organization.slug, atendimento=atendimento.external_id
    )

    try:
        if client is not None:
            text = client.judge(system=system_prompt, user=transcript, model=model)
        else:
            if not settings.QA_LLM_ENABLED:
                return None
            from apps.integrations.gemini.client import GeminiClient

            with GeminiClient(api_key=settings.GEMINI_API_KEY) as c:
                text = c.judge(system=system_prompt, user=transcript, model=model)
        data = _parse_review(text)
    except Exception:
        log.warning("qa_review_failed", exc_info=True)
        return None

    if is_bot:
        # Guarda o JSON nomeado do bot em `raw` (auditoria) e persiste as colunas.
        raw = {**data, "judge_kind": "bot"}
        return _persist(
            organization, atendimento, _bot_data_to_columns(data), model, raw=raw
        )
    return _persist(organization, atendimento, data, model)


def _persist(
    organization: Organization,
    atendimento: Atendimento,
    data: dict[str, Any],
    model: str,
    *,
    raw: dict[str, Any] | None = None,
) -> QAReview:
    review, _created = QAReview.objects.update_or_create(
        organization=organization,
        atendimento=atendimento,
        defaults={
            "resolveu": bool(data.get("resolveu")),
            "sla_ok": bool(data.get("sla_ok")),
            "tom": _coerce_int(data.get("tom"), lo=1, hi=5),
            "empatia": _coerce_int(data.get("empatia"), lo=1, hi=5),
            "aderencia": _coerce_int(data.get("aderencia"), lo=1, hi=5),
            "overall_score": _coerce_int(data.get("overall_score"), lo=0, hi=100) or 0,
            "categoria": str(data.get("categoria", ""))[:64],
            "resumo": str(data.get("resumo", "")),
            "melhoria": str(data.get("melhoria", "")),
            "atendente_external_id": atendimento.atendente_external_id,
            "atendente_nome": atendimento.atendente_nome,
            "model_name": model,
            "raw": raw if raw is not None else data,
            "reviewed_at": timezone.now(),
        },
    )
    return review


@allow_cross_tenant(reason="seleção de QA roda em Celery, escopo é a org passada")
def select_conversations_for_qa(
    organization: Organization, *, limit: int = 20
) -> list[Atendimento]:
    """Amostra conversas fechadas ainda não avaliadas, priorizando as piores.

    Ordena por nota do cliente ascendente (notas baixas primeiro; sem nota vão
    pro fim) e depois pelas mais recentes. Mantém o lote pequeno pra controlar
    custo de LLM. A amostragem pode ser enriquecida depois (reincidentes,
    aleatório) sem mudar a interface.
    """
    reviewed_ids = QAReview.objects.filter(
        organization=organization
    ).values_list("atendimento_id", flat=True)
    qs = (
        Atendimento.objects.filter(
            organization=organization, status=Atendimento.Status.CLOSED
        )
        .exclude(id__in=reviewed_ids)
        .select_related("departamento")
        .order_by("rating", "-opened_at")
    )
    return list(qs[:limit])
