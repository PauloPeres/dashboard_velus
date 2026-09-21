"""Montagem dos dados da aba Quedas & Massivas (#146).

A aba é a primeira tela **operacional** do dashboard: quem usa está no meio de
um rompimento, com o telefone tocando. Por isso tudo aqui é foto de agora —
nada responde a filtro de período, e o único passado que a página mostra é o
registro genérico das massivas já encerradas (§1 do `docs/massivas-plano.md`).

O que este módulo faz de diferente de uma agregação comum é **não deixar um
número sair sozinho**. Três limites medidos em produção obrigam a isso:

- o maior evento real do dia fecha em escopo OLT com fração de 0,02 (§2.6): a
  OLT está 98% de pé e o que rompeu é um trecho abaixo dela. Então o rótulo do
  elemento nunca é devolvido sem a fração ao lado, e quando há trecho suspeito é
  ele que vem marcado como destaque;
- a geometria do cabo não existe na API (§2.3), então nada aqui produz a palavra
  "cabo": o campo é `suspected_segment_label`, um trecho entre caixas;
- `motivo_desconexao` está vazio em ~70% das quedas (§2.4), então o painel de
  motivos devolve a cobertura junto com a distribuição — quem soma as fatias tem
  que ver que elas não fecham o total.

E, como a tela é de tempo real, ela precisa dizer **de quando é a foto**:
`ConnectionPollState` é lido em toda página e o silêncio nunca é lido como
"ninguém caiu".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.customers.infrastructure.models import Contract
from apps.network.infrastructure.models import (
    Connection,
    ConnectionDropEvent,
    ConnectionPollState,
    NetworkElement,
    NetworkElementGeometry,
    OutageAffectedLogin,
    OutageEvent,
)

# Bucket da linha do tempo — o mesmo do detector (§5.1), pra que uma barra alta
# da tela corresponda a um cluster que o detector viu.
BUCKET_MINUTES = 10

# Janela da linha do tempo. Curta de propósito: é tela de rompimento em curso,
# não série histórica.
TIMELINE_HOURS = 6

# O poll roda de 3 em 3 min (§7). Passou disto, a foto está velha o bastante
# para a tela avisar em vez de deixar o usuário supor que ninguém caiu.
POLL_STALE_MINUTES = 10

# Piora de sinal de retorno que sugere fusão mal feita (§2.8). Uma ONU saudável
# variou menos de 0,7 dB em dez dias, então 3 dB é inequívoco.
SIGNAL_DEGRADATION_DB = 3.0

# Campos que a #148 traz. Enquanto não existirem, a tabela mostra "sem leitura"
# em vez de quebrar — as duas issues correm em paralelo.
_SIGNAL_FIELD_NAMES = (
    "signal_rx_before",
    "signal_before_measured_at",
    "signal_rx_after",
    "signal_after_measured_at",
)

# Como cada escopo se chama numa frase. "2% dos logins da OLT" só é legível se o
# substantivo vier junto do número.
_SCOPE_NOUN = {
    OutageEvent.Scope.CTO: "caixa",
    OutageEvent.Scope.PON: "porta PON",
    OutageEvent.Scope.OLT: "OLT",
    OutageEvent.Scope.POP: "POP",
    OutageEvent.Scope.GEO: "área",
}

# Escopos em que fração baixa NÃO significa "o elemento caiu" — é o caso do
# evento real de 19:46 (§2.6). CTO fica de fora: lá a fração alta é o próprio
# critério de escopo.
_SCOPES_QUE_AGREGAM = {
    OutageEvent.Scope.PON,
    OutageEvent.Scope.OLT,
    OutageEvent.Scope.POP,
}

# Abaixo disto, dizer o nome do elemento sem ressalva manda o técnico ao lugar
# errado.
_FRACAO_QUE_EXIGE_RESSALVA = 0.5


# =============================================================================
# De quando é a foto (regra 4)
# =============================================================================
def poll_snapshot(org: Any, *, now: datetime) -> dict[str, Any]:
    """Estado do poll de status — a tela declara a idade do que mostra."""
    state = ConnectionPollState.objects.filter(organization=org).first()
    if state is None:
        return {
            "observando": False,
            "ok": False,
            "mensagem": (
                "O poll de status de conexão ainda não rodou nesta organização. "
                "A tela não sabe quem está fora agora — a ausência de massivas "
                "abaixo não quer dizer que ninguém caiu."
            ),
        }

    ultimo_sucesso = state.last_success_at
    ultima_tentativa = state.last_poll_at
    idade_min = (
        int((now - ultimo_sucesso).total_seconds() // 60) if ultimo_sucesso else None
    )
    # O poll grava `last_poll_at` sempre e `last_success_at` só quando a leitura
    # do IXC volta. Tentativa mais nova que o sucesso = a última rodada falhou.
    falhou = bool(
        ultima_tentativa and (ultimo_sucesso is None or ultima_tentativa > ultimo_sucesso)
    )
    velha = idade_min is None or idade_min > POLL_STALE_MINUTES

    if ultimo_sucesso is None:
        mensagem = (
            "Nenhuma leitura de status concluiu com sucesso até agora. "
            "O que aparece abaixo não é a situação de agora."
        )
    elif falhou:
        mensagem = (
            f"A última tentativa de leitura falhou. A foto abaixo é de "
            f"{ultimo_sucesso:%d/%m %H:%M} ({idade_min} min atrás) — quedas "
            f"ocorridas depois disso ainda não apareceram aqui."
        )
    elif velha:
        mensagem = (
            f"Última leitura bem-sucedida em {ultimo_sucesso:%d/%m %H:%M} "
            f"({idade_min} min atrás). O poll deveria rodar a cada 3 min — "
            f"a foto está velha."
        )
    else:
        mensagem = f"Foto de {ultimo_sucesso:%d/%m %H:%M} ({idade_min} min atrás)."

    return {
        "observando": True,
        "ok": not (falhou or velha),
        "falhou": falhou,
        "velha": velha,
        "baseline_at": state.baseline_at,
        "last_poll_at": ultima_tentativa,
        "last_success_at": ultimo_sucesso,
        "idade_min": idade_min,
        "mensagem": mensagem,
    }


# =============================================================================
# Escopo + fração: nunca um sem o outro (regra 1)
# =============================================================================
def _fracao_str(fracao: float) -> str:
    """'2%' — com piso explícito pra não arredondar 0,4% pra 0%."""
    pct = fracao * 100
    if 0 < pct < 1:
        return "<1%"
    return f"{pct:.0f}%"


def outage_row(
    outage: OutageEvent,
    *,
    referencia: str = "",
    veredito: dict[str, Any] | None = None,
    vizinhanca: dict[str, Any] | None = None,
    reincidencia: dict[str, Any] | None = None,
    cabos: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Uma massiva pronta pro template, com escopo e fração já costurados.

    O domínio guarda `element_label` e `affected_fraction` separados de propósito
    (docstring de `OutageEvent`); é aqui que eles voltam a andar juntos, porque
    é aqui que se sabe que a tela tem espaço pra frase inteira.

    `referencia` é o lugar humano do elemento em escopo ("no POP Portal do
    Pirapora"), derivado do cadastro por `element_references`. Vem de fora
    porque a derivação é uma query e esta função é chamada em laço.

    `veredito` (R7) e `vizinhanca` (R6) também vêm de fora, e pelo mesmo motivo:
    dependem das quedas da massiva, que são outra query. `reincidencia` (R8) vem
    de fora por um motivo diferente: ela olha o passado do elemento, então é uma
    contagem só para a tela inteira. Ausentes, a linha sai como sempre saiu.
    """
    escopo_nome = _SCOPE_NOUN.get(outage.scope, outage.scope)
    fracao = outage.affected_fraction or 0.0
    fracao_str = _fracao_str(fracao)
    # Fração zero com gente fora **não é zero** — é denominador ausente. Uma
    # massiva de 121 clientes exibindo "0% dos logins das caixas envolvidas" foi
    # o que apareceu em produção: eram quedas sem CTO no snapshot, então não
    # havia caixa de onde tirar o denominador. Zero ali se lê como medida
    # ("quase ninguém caiu") e é o oposto do que aconteceu.
    sem_denominador = fracao <= 0 and (outage.affected_count or 0) > 0

    elemento = outage.element_label or "elemento não identificado"
    # A frase é sempre "quem" + "quanto daquilo". Nunca só "quem".
    #
    # GEO é a exceção e precisa dizer o denominador por extenso: o cluster de
    # proximidade não é elemento de cadastro, então "X% dos logins da área" faria
    # parecer que existe uma área com logins contáveis. O que o detector divide
    # ali é pelas caixas que entraram no agrupamento — e é isso que vai escrito.
    if sem_denominador:
        elemento_frase = (
            f"{elemento} — sem denominador no cadastro para dizer que fração caiu"
        )
    elif outage.scope == OutageEvent.Scope.GEO:
        elemento_frase = f"{elemento} — {fracao_str} dos logins das caixas envolvidas"
    elif outage.element_label:
        elemento_frase = f"{elemento} — {fracao_str} dos logins da {escopo_nome}"
    else:
        elemento_frase = f"Sem elemento em escopo — agrupamento por {escopo_nome}"

    trecho = outage.suspected_segment_label or ""
    ressalva = ""
    if sem_denominador:
        ressalva = (
            "As quedas desta massiva não têm caixa no cadastro, então não há "
            "denominador: dá para dizer quantos caíram, não que parte do "
            "elemento isso representa."
        )
    elif outage.scope in _SCOPES_QUE_AGREGAM and fracao < _FRACAO_QUE_EXIGE_RESSALVA:
        resto = _fracao_str(1 - fracao)
        ressalva = (
            f"{resto} dos logins desta {escopo_nome} continuam no ar: o que caiu "
            f"é um trecho abaixo dela, não a {escopo_nome} inteira."
        )

    afetados = outage.affected_count or 0
    restaurados = outage.restored_count or 0

    return {
        "id": outage.pk,
        "scope": outage.scope,
        "scope_label": outage.get_scope_display(),
        "escopo_nome": escopo_nome,
        "element_label": outage.element_label,
        "elemento_frase": elemento_frase,
        "fracao": fracao,
        "fracao_str": fracao_str,
        "sem_denominador": sem_denominador,
        "ressalva_escopo": ressalva,
        # Quando há trecho, é ELE o destaque: é ele que diz pra onde o técnico
        # vai. O elemento em escopo vira contexto secundário.
        "trecho_suspeito": trecho,
        "tem_trecho": bool(trecho),
        # O que representa a massiva quando só cabe UMA linha (KPI, título): o
        # trecho quando existe, senão o elemento — que nunca vai sem a fração.
        "destaque": trecho or elemento_frase,
        "confidence": outage.confidence,
        "confidence_label": outage.get_confidence_display(),
        "started_at": outage.started_at,
        "ended_at": outage.ended_at,
        "duracao_min": _duracao_min(outage),
        "affected_count": afetados,
        "restored_count": restaurados,
        "ainda_fora": max(afetados - restaurados, 0),
        "restored_pct": round(outage.restored_fraction * 100),
        "referencia": referencia,
        "mrr_at_risk": outage.mrr_at_risk or Decimal("0"),
        "is_open": outage.is_open,
        "veredito": veredito,
        "vizinhanca": vizinhanca,
        "reincidencia": reincidencia,
        "cabos": cabos,
        # Causa confirmada (R9) e manutenção programada (R10). A causa NÃO
        # substitui o veredito na tela: é a divergência entre os dois que mede
        # o quanto o palpite automático acerta.
        "confirmed_cause": outage.confirmed_cause,
        "confirmed_cause_label": outage.get_confirmed_cause_display()
        if outage.confirmed_cause
        else "",
        "cause_tags": tag_labels(outage.cause_tags),
        "cause_note": outage.cause_note,
        "cause_confirmed_at": outage.cause_confirmed_at,
        "cause_confirmed_by": (
            outage.cause_confirmed_by.email if outage.cause_confirmed_by_id else ""
        ),
        "is_expected": outage.is_expected,
        "maintenance_label": outage.maintenance_label,
        # Reconhecimento (P8): "alguém já assumiu". Vale para a tela e para o
        # painel, que para de interromper quando isto existe.
        "acknowledged_at": outage.acknowledged_at,
        "acknowledged_by": (
            outage.acknowledged_by.email if outage.acknowledged_by_id else ""
        ),
        "is_acknowledged": outage.is_acknowledged,
    }


def _duracao_min(outage: OutageEvent) -> int | None:
    if outage.ended_at is None:
        return None
    return int((outage.ended_at - outage.started_at).total_seconds() // 60)


# =============================================================================
# Tela principal
# =============================================================================
_CAMPOS_DE_QUEDA_NO_MAPA = (
    "id", "login", "dropped_at", "restored_at", "reason", "cto_external_id",
    "latitude", "longitude", "monthly_amount",
)


def compute_massivas_agora(org: Any, *, now: datetime) -> dict[str, Any]:
    """KPIs de agora, massivas abertas, motivos e pontos do mapa."""
    quedas_abertas = list(
        ConnectionDropEvent.objects.filter(organization=org, restored_at__isnull=True)
        .select_related("connection")
        .only(*_CAMPOS_DE_QUEDA_NO_MAPA, "connection", "connection__onu_last_drop_cause")
    )

    # "Ver cliente voltando ao vivo" é metade da razão de ser da tela: quem
    # voltou nas últimas horas continua no mapa, em verde, pra que a equipe veja
    # o vermelho virar verde durante o reparo. Sem isso o retorno seria invisível
    # — o cliente simplesmente sumiria do mapa.
    voltaram = list(
        ConnectionDropEvent.objects.filter(
            organization=org, restored_at__gte=now - timedelta(hours=TIMELINE_HOURS)
        ).only(*_CAMPOS_DE_QUEDA_NO_MAPA)
    )

    # O mapa é das MASSIVAS ABERTAS, não de toda queda solta da base. Numa base
    # grande sempre há dezenas de quedas individuais espalhadas — ONU desligada
    # na tomada, cliente que mudou de casa — e elas enchiam o mapa de vermelho
    # sem relação nenhuma com o rompimento que a equipe está atendendo. O
    # vínculo queda↔massiva já existe em `OutageAffectedLogin`; o mapa passa a
    # sair de lá. Quem voltou continua aparecendo (em verde), porque é dentro da
    # massiva que o esverdeamento conta a história do reparo.
    afetados_de_massiva = list(
        OutageAffectedLogin.objects.filter(
            organization=org, outage__ended_at__isnull=True
        )
        .select_related("drop_event", "drop_event__connection")
        .only(
            "drop_event",
            *(f"drop_event__{c}" for c in _CAMPOS_DE_QUEDA_NO_MAPA),
            # A causa da ONU e a porta PON moram na Connection: insumos do
            # veredito (R7) e da vizinhança (R6).
            "drop_event__connection",
            "drop_event__connection__onu_last_drop_cause",
            "drop_event__connection__pon_external_id",
        )
    )
    # `drop_event` é SET_NULL: a queda pode ter sido podada e a massiva
    # sobreviver. Sem queda não há coordenada, então o afetado não vira ponto.
    quedas_no_mapa = [a.drop_event for a in afetados_de_massiva if a.drop_event]

    # Quantas quedas em curso ficaram FORA do mapa por não pertencerem a
    # nenhuma massiva aberta. O número tem que aparecer na tela: um mapa com 8
    # pontos sobre 40 clientes fora seria lido como "a massiva é pequena".
    ids_em_massiva = {a.drop_event_id for a in afetados_de_massiva}
    quedas_avulsas = sum(1 for q in quedas_abertas if q.pk not in ids_em_massiva)

    abertas = list(
        OutageEvent.objects.filter(organization=org, ended_at__isnull=True).order_by(
            "-affected_count", "started_at"
        )
    )
    referencias = element_references(org, abertas)
    # As quedas já vieram na query dos afetados; agrupar em memória evita uma
    # consulta por massiva dentro do laço.
    quedas_por_massiva: dict[int, list[ConnectionDropEvent]] = {}
    for afetado in afetados_de_massiva:
        if afetado.drop_event is not None:
            quedas_por_massiva.setdefault(afetado.outage_id, []).append(afetado.drop_event)
    vizinhancas = {
        o.pk: compute_vizinhanca(org, quedas_por_massiva.get(o.pk, [])) for o in abertas
    }
    reincidencias = compute_reincidencia(org, abertas, now=now)
    # Uma leitura só dos traçados para todas as massivas abertas.
    tracados = tracados_de_cabo(org) if abertas else []
    cabos_por_massiva = {
        o.pk: compute_cabos_candidatos(
            org, quedas_por_massiva.get(o.pk, []), tracados=tracados
        )
        for o in abertas
    }
    linhas = [
        outage_row(
            o,
            referencia=referencias.get(o.pk, ""),
            veredito=compute_veredito(quedas_por_massiva.get(o.pk, []), scope=o.scope),
            vizinhanca=vizinhancas.get(o.pk),
            reincidencia=reincidencias.get(o.pk),
            cabos=cabos_por_massiva.get(o.pk),
        )
        for o in abertas
    ]
    # A mensagem do técnico depende da linha pronta (ela repete o destaque, o
    # cabo e o veredito), por isso vem depois e não dentro de `outage_row`.
    for linha, o in zip(linhas, abertas, strict=True):
        linha["mensagem_tecnico"] = compute_mensagem_tecnico(
            org, linha, quedas_por_massiva.get(o.pk, []), now=now
        )
    # As caixas que escaparam entram no mapa como ponto vazado. Vêm de todas as
    # massivas abertas, sem repetir a mesma caixa.
    intactas_no_mapa: dict[str, dict[str, Any]] = {}
    for vizinhanca in vizinhancas.values():
        for irma in vizinhanca.get("intactas", []):
            intactas_no_mapa.setdefault(irma["cto"], irma)

    mensalidade_afetada = sum((o.mrr_at_risk or Decimal("0")) for o in abertas)
    maior = linhas[0] if linhas else None

    return {
        "clientes_fora": len(quedas_abertas),
        "clientes_que_voltaram": len(voltaram),
        "massivas_abertas": len(abertas),
        "mensalidade_afetada": mensalidade_afetada,
        "maior_massiva": maior,
        "linhas": linhas,
        "causas_onu": compute_causas_onu(quedas_abertas),
        "motivos": compute_motivos(quedas_abertas),
        "mapa": compute_mapa(
            org,
            quedas_no_mapa,
            vizinhas_intactas=list(intactas_no_mapa.values()),
            # O mapa da tela geral desenha os candidatos de todas as massivas
            # abertas, sem repetir cabo: é o mesmo critério dos pontos.
            cabos_candidatos=sorted(
                {
                    cabo_id
                    for c in cabos_por_massiva.values()
                    for cabo_id in c.get("ids_no_mapa", [])
                }
            ),
        ),
        "mapa_quedas_avulsas": quedas_avulsas,
        "mapa_voltaram": sum(1 for q in quedas_no_mapa if q.restored_at is not None),
        "timeline": compute_timeline(org, now=now),
        "janela_retorno_horas": TIMELINE_HOURS,
    }


# =============================================================================
# Onde fica, em português de técnico (regra 1, parte que faltava)
# =============================================================================
def element_references(
    org: Any, outages: list[OutageEvent]
) -> dict[int, str]:
    """Lugar humano do elemento em escopo — "no POP Portal do Pirapora".

    "OLT 1 — 2% dos logins da OLT" diz *quanto*, mas não diz pra onde ir. O
    cadastro tem o que falta: `NetworkElement` guarda nome, endereço e a
    hierarquia por `(parent_kind, parent_external_id)`, então dá pra subir da
    OLT ao POP e devolver um nome que existe no mundo.

    Só enriquece escopo OLT e POP: CTO já vem com o código da caixa (que o
    técnico conhece) e GEO não tem elemento. Quando o cadastro não fecha, a
    função devolve string vazia — a tela fica como estava, e nada de inventar
    localização.
    """
    alvos = [
        o for o in outages
        if o.element_external_id
        and o.scope in (OutageEvent.Scope.OLT, OutageEvent.Scope.POP)
    ]
    if not alvos:
        return {}

    elementos = {
        (e.kind, e.external_id): e
        for e in NetworkElement.objects.filter(
            organization=org,
            kind__in=[NetworkElement.Kind.OLT, NetworkElement.Kind.POP],
            external_id__in={o.element_external_id for o in alvos},
        )
    }
    # As OLTs apontam pro POP pelo par (parent_kind, parent_external_id) — uma
    # segunda query resolve todos os pais de uma vez.
    pais = {
        o.element_external_id
        for o in alvos
        if (elemento := elementos.get((o.scope, o.element_external_id)))
        and elemento.parent_kind == NetworkElement.Kind.POP
        and elemento.parent_external_id
    }
    pops_por_id: dict[str, NetworkElement] = {}
    if pais:
        ids_pais = {
            elementos[(o.scope, o.element_external_id)].parent_external_id
            for o in alvos
            if (o.element_external_id in pais)
        }
        pops_por_id = {
            e.external_id: e
            for e in NetworkElement.objects.filter(
                organization=org,
                kind=NetworkElement.Kind.POP,
                external_id__in=ids_pais,
            )
        }

    saida: dict[int, str] = {}
    for o in alvos:
        elemento = elementos.get((o.scope, o.element_external_id))
        if elemento is None:
            continue
        if o.scope == OutageEvent.Scope.POP:
            saida[o.pk] = _referencia_de(elemento, prefixo="POP")
            continue
        pop = pops_por_id.get(elemento.parent_external_id)
        referencia = _referencia_de(pop, prefixo="no POP") if pop else ""
        # Sem POP no cadastro, o endereço da própria OLT ainda ajuda.
        saida[o.pk] = referencia or _referencia_de(elemento, prefixo="em")
    return {pk: texto for pk, texto in saida.items() if texto}


def _referencia_de(elemento: NetworkElement | None, *, prefixo: str) -> str:
    if elemento is None:
        return ""
    # Nome vazio ou igual ao id não é referência humana — é o mesmo número de
    # novo, e repeti-lo daria falsa sensação de localização.
    nome = (elemento.name or "").strip()
    if nome and nome != elemento.external_id:
        return f"{prefixo} {nome}"
    endereco = (elemento.address or "").strip()
    return f"{prefixo} {endereco}" if endereco else ""


def compute_motivos(quedas: list[ConnectionDropEvent]) -> dict[str, Any]:
    """Distribuição de motivos **com a cobertura declarada** (regra 3, §2.4).

    Devolver só as fatias seria mentir por omissão: no IXC medido, 165 de 237
    quedas não têm motivo nenhum. A cobertura vem junto pra que a leitura seja
    "motivo conhecido em N de M", nunca "a causa das quedas é esta".
    """
    total = len(quedas)
    contagem: dict[str, int] = {}
    for q in quedas:
        motivo = (q.reason or "").strip()
        if not motivo:
            continue
        contagem[motivo] = contagem.get(motivo, 0) + 1

    conhecidos = sum(contagem.values())
    linhas = [
        {
            "motivo": motivo,
            "quedas": n,
            # Percentual SOBRE OS CONHECIDOS, e a tela diz isso — dividir pelo
            # total daria a impressão de que os 70% sem motivo são "outros".
            "pct_dos_conhecidos": round(n / conhecidos * 100) if conhecidos else 0,
        }
        for motivo, n in sorted(contagem.items(), key=lambda kv: -kv[1])
    ]
    return {
        "total": total,
        "conhecidos": conhecidos,
        "sem_motivo": total - conhecidos,
        "cobertura_pct": round(conhecidos / total * 100) if total else 0,
        "linhas": linhas,
    }


# Valores que a OLT usa para dizer "não informei". Vazio já é ausência; "-" é o
# sentinela do IXC. Nenhum dos dois pode virar fatia da distribuição.
_CAUSAS_AUSENTES = {"", "-"}


def compute_causas_onu(quedas: list[ConnectionDropEvent]) -> dict[str, Any]:
    """Distribuição da **causa que a OLT reportou**, com cobertura declarada.

    Este é o painel principal desde a #148: ao contrário do `motivo_desconexao`
    do RADIUS (que só sabe dizer `NAS-Request` ou vazio), `causa_ultima_queda`
    discrimina o tipo de falha — e é ele que responde "mando técnico ou não".
    Vem da listagem passiva, sem nenhuma consulta à OLT.

    O código **não interpreta**: os rótulos saem crus, como a OLT escreveu. O
    que significa `dying-gasp` numa massiva é leitura do time, e a legenda
    estática do template ajuda a ler sem que a tela conclua nada.

    A cobertura vai junto pelo mesmo motivo do painel do RADIUS — na amostra de
    300 ONUs de produção só ~15% traziam causa, mas aquela amostra inclui ONU
    morta há meses. Numa massiva viva a tendência é ser melhor, e é justamente
    por isso que o número tem que aparecer, não ser presumido.
    """
    total = len(quedas)
    contagem: dict[str, int] = {}
    for q in quedas:
        # `getattr` porque a causa mora na Connection e a queda pode ter chegado
        # sem ela carregada; ausente é ausência, nunca zero-valor inventado.
        conexao = getattr(q, "connection", None)
        causa = (getattr(conexao, "onu_last_drop_cause", "") or "").strip()
        if causa in _CAUSAS_AUSENTES:
            continue
        contagem[causa] = contagem.get(causa, 0) + 1

    conhecidos = sum(contagem.values())
    linhas = [
        {
            "causa": causa,
            "quedas": n,
            "pct_dos_conhecidos": round(n / conhecidos * 100) if conhecidos else 0,
        }
        for causa, n in sorted(contagem.items(), key=lambda kv: -kv[1])
    ]
    return {
        "total": total,
        "conhecidos": conhecidos,
        "sem_causa": total - conhecidos,
        "cobertura_pct": round(conhecidos / total * 100) if total else 0,
        "linhas": linhas,
    }


# =============================================================================
# Veredito de causa provável (R7)
# =============================================================================
# `dying-gasp` é a ONU avisando que perdeu ENERGIA antes de desligar; `LOS`,
# `LOSi` e `LOBi` são perda de SINAL ÓPTICO. A diferença decide a ação: numa
# massiva de dying-gasp não se manda viatura, espera-se a concessionária; onde
# aparece LOS, é fibra e a equipe sai.
_CAUSAS_DE_ENERGIA = {"dying-gasp"}
_CAUSAS_DE_FIBRA = {"los", "losi", "lobi", "losi/lobi"}

# Abaixo disso o veredito não sai. Não é rigor estatístico — é que 2 de 3 quedas
# com causa conhecida viram "67% de energia", um número que parece medida e é
# coincidência.
_MIN_QUEDAS_PARA_VEREDITO = 5
_MIN_COBERTURA_PARA_VEREDITO = 40
# Concentração necessária pra nomear a causa. Entre os dois limiares o veredito
# é "misto", que é uma resposta legítima: massiva pode ter as duas coisas.
_PCT_PARA_NOMEAR_CAUSA = 70


def compute_veredito(quedas: list[ConnectionDropEvent], *, scope: str = "") -> dict[str, Any]:
    """Causa provável de uma massiva — ou a recusa explícita de opinar.

    Combina o que já existe: a causa que a OLT reportou (#148), o escopo em que o
    detector fechou o evento e a cronologia das quedas. O veredito é **provável**
    e diz em cima do que foi formado; quando a base não dá, ele se recusa a
    concluir em vez de arredondar para o palpite mais próximo.

    Sobre a cronologia: ela entra como *descrição*, não como prova. A literatura
    de NOC diz que rompimento derruba todo mundo no mesmo segundo e que falta de
    energia com nobreak derruba escalonado — mas isso **não se confirmou nos
    nossos dados** (medição de 2026-09-18, 24 massivas): o evento com maior
    proporção de LOS é justamente o de maior espalhamento (337 s), e massivas de
    dying-gasp puro fecham em 20 s. O `ultima_conexao_final` do IXC parece
    registrar quando a OLT reportou, não quando o cliente caiu. Então a tela
    mostra o espalhamento e deixa a leitura com quem opera, em vez de derivar
    conclusão de um sinal que não se sustentou aqui.
    """
    total = len(quedas)
    energia = 0
    fibra = 0
    for q in quedas:
        conexao = getattr(q, "connection", None)
        causa = (getattr(conexao, "onu_last_drop_cause", "") or "").strip()
        if causa in _CAUSAS_AUSENTES:
            continue
        normalizada = causa.lower()
        if normalizada in _CAUSAS_DE_ENERGIA:
            energia += 1
        elif normalizada in _CAUSAS_DE_FIBRA:
            fibra += 1

    conhecidos = energia + fibra
    cobertura = round(conhecidos / total * 100) if total else 0
    pct_energia = round(energia / conhecidos * 100) if conhecidos else 0
    pct_fibra = round(fibra / conhecidos * 100) if conhecidos else 0

    if total < _MIN_QUEDAS_PARA_VEREDITO or cobertura < _MIN_COBERTURA_PARA_VEREDITO:
        veredito, rotulo = "sem_base", "sem base para opinar"
        base = (
            f"causa conhecida em {conhecidos} de {total} quedas — "
            "pouco para separar energia de fibra"
        )
    elif pct_energia >= _PCT_PARA_NOMEAR_CAUSA:
        veredito, rotulo = "energia", "provável falta de energia"
        base = f"{pct_energia}% das quedas com causa conhecida reportaram dying-gasp"
    elif pct_fibra >= _PCT_PARA_NOMEAR_CAUSA:
        veredito, rotulo = "fibra", "provável problema de fibra"
        base = f"{pct_fibra}% das quedas com causa conhecida reportaram perda de sinal óptico"
    else:
        veredito, rotulo = "misto", "sinais misturados"
        base = (
            f"{pct_energia}% dying-gasp e {pct_fibra}% perda de sinal entre as "
            "quedas com causa conhecida — não é uma coisa só"
        )

    # O escopo é o segundo sinal: rompimento correlaciona com TOPOLOGIA (a queda
    # segue a árvore da rede), falta de energia correlaciona com GEOGRAFIA (segue
    # o alimentador da concessionária, pegando clientes de PONs diferentes). Não
    # muda o veredito — a causa da ONU é evidência mais direta —, mas quando os
    # dois apontam junto vale dizer, e quando se contradizem vale mais ainda.
    reforco = ""
    if veredito == "energia" and scope == "GEO":
        reforco = "O agrupamento é geográfico, o que combina com queda de energia na área."
    elif veredito == "energia" and scope in ("OLT", "PON", "CTO"):
        reforco = (
            "Mas o agrupamento fechou por topologia, não por geografia — se fosse "
            "a concessionária, esperaria-se gente de outras PONs junto."
        )
    elif veredito == "fibra" and scope in ("OLT", "PON", "CTO"):
        reforco = "O agrupamento fechou por topologia, o que combina com rompimento."

    return {
        "veredito": veredito,
        "rotulo": rotulo,
        "base": base,
        "reforco": reforco,
        "total": total,
        "conhecidos": conhecidos,
        "cobertura_pct": cobertura,
        "pct_energia": pct_energia,
        "pct_fibra": pct_fibra,
        "cronologia": _cronologia(quedas),
    }


def _cronologia(quedas: list[ConnectionDropEvent]) -> dict[str, Any]:
    """Espalhamento das quedas — descrição factual, sem conclusão.

    91% das quedas trazem o horário real do IXC (`ultima_conexao_final`); o resto
    cai no relógio do poll, e aí o horário é sintético. Os sintéticos saem da
    conta: na partida a frio de 09/09, 189 quedas ficaram com o mesmo timestamp
    ao microssegundo, o que leria como "todas no mesmo instante" sendo que é
    ausência de dado. Timestamp do IXC vem sempre com microssegundo zero.
    """
    reais = sorted(
        q.dropped_at for q in quedas
        if q.dropped_at is not None and q.dropped_at.microsecond == 0
    )
    if len(reais) < 2:
        return {"medivel": False, "com_hora_real": len(reais), "total": len(quedas)}
    spread = int((reais[-1] - reais[0]).total_seconds())
    return {
        "medivel": True,
        "com_hora_real": len(reais),
        "total": len(quedas),
        "spread_segundos": spread,
        "spread_str": f"{spread} s" if spread < 120 else f"{spread // 60} min",
        "primeira": reais[0],
        "ultima": reais[-1],
    }


# =============================================================================
# Vizinhança: quem NÃO caiu no mesmo caminho (R6)
# =============================================================================
# "Caíram 31 clientes" não diz onde procurar. "As 3 caixas da PON 364 estão
# 100% fora e a CTO 1288, na mesma PON, está 9/9 no ar" diz: o problema está
# entre a 1288 e as outras três. Quem ficou de pé delimita o trecho tão bem
# quanto quem caiu — e some da tela se ninguém for buscar.
#
# A vizinhança é derivada pela PON, e a PON é propriedade do LOGIN, não da caixa
# (§2.5c: 26% das CTOs são alimentadas por mais de uma porta). Então as PONs da
# massiva saem dos logins que caíram, e as caixas irmãs são as que têm login
# nessas mesmas PONs.


def compute_vizinhanca(org: Any, quedas: list[ConnectionDropEvent]) -> dict[str, Any]:
    """Caixas da mesma PON que a massiva não levou — e quanto de cada uma caiu.

    Devolve `determinavel=False` quando a PON dos afetados não é conhecida. Sem
    PON não há irmã: cair para "mesma OLT" encheria a tela com centenas de
    caixas que não têm relação nenhuma com o trecho, o que é pior do que não
    responder.
    """
    from apps.network.application.outage_detection import active_logins_per_cto

    ctos_afetadas = {q.cto_external_id for q in quedas if q.cto_external_id}
    pons = set()
    for q in quedas:
        conexao = getattr(q, "connection", None)
        pon = (getattr(conexao, "pon_external_id", "") or "").strip()
        if pon:
            pons.add(pon)

    if not pons:
        return {
            "determinavel": False,
            "motivo": (
                "As quedas desta massiva não têm porta PON no cadastro, então "
                "não dá para dizer quais caixas dividem o mesmo caminho."
            ),
            "ctos_afetadas": len(ctos_afetadas),
        }

    # Caixas que têm login em alguma das PONs da massiva e não estão entre as
    # afetadas: são as candidatas a delimitar o trecho.
    irmas_ids = set(
        Connection.objects.filter(organization=org, pon_external_id__in=pons)
        .exclude(cto_external_id="")
        .exclude(cto_external_id__in=ctos_afetadas)
        .values_list("cto_external_id", flat=True)
        .distinct()
    )

    denominadores = active_logins_per_cto()
    elementos = {
        e.external_id: e
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in=irmas_ids | ctos_afetadas,
        ).only("external_id", "name", "latitude", "longitude")
    }

    # Quantos de cada caixa irmã estão fora AGORA — uma queda aberta é queda
    # aberta, pertença ela a esta massiva ou a outra.
    fora_por_cto: dict[str, int] = {}
    for cto_id in ConnectionDropEvent.objects.filter(
        organization=org, restored_at__isnull=True, cto_external_id__in=irmas_ids
    ).values_list("cto_external_id", flat=True):
        fora_por_cto[cto_id] = fora_por_cto.get(cto_id, 0) + 1

    irmas = []
    for cto_id in sorted(irmas_ids):
        total = denominadores.get(cto_id, 0)
        fora = fora_por_cto.get(cto_id, 0)
        elemento = elementos.get(cto_id)
        irmas.append({
            "cto": cto_id,
            "nome": (elemento.name if elemento else "") or cto_id,
            # O mapa desenha a caixa intacta como ponto vazado (R3); sem
            # coordenada ela ainda aparece na lista, só não no mapa.
            "lat": elemento.latitude if elemento else None,
            "lon": elemento.longitude if elemento else None,
            "fora": fora,
            "total": total,
            "de_pe": max(total - fora, 0),
            # Caixa intacta é a que mais informa: é ela que marca o limite do
            # trecho. Por isso vira flag e não só um número a comparar.
            "intacta": fora == 0 and total > 0,
        })
    # Intactas primeiro, depois as maiores: a leitura útil é "quem escapou".
    irmas.sort(key=lambda i: (not i["intacta"], -i["total"]))

    intactas = [i for i in irmas if i["intacta"]]
    return {
        "determinavel": True,
        "intactas": intactas,
        "pons": sorted(pons),
        "ctos_afetadas": len(ctos_afetadas),
        "irmas": irmas,
        "n_irmas": len(irmas),
        "n_intactas": len(intactas),
        # Só das INTACTAS: é esse número que a frase da tela usa ("1 caixa
        # continua 100% no ar, com 9 logins de pé"). Somar as parciais aqui
        # faria a intacta parecer maior do que é.
        "logins_de_pe": sum(i["de_pe"] for i in intactas),
        "sem_denominador": sum(1 for i in irmas if i["total"] == 0),
    }


# =============================================================================
# Reincidência do trecho (R8)
# =============================================================================
# "Esta OLT teve 4 massivas" separa o acidente do trecho cronicamente ruim —
# travessia de rodovia, poste de esquina, emenda mal feita que abre a cada
# chuva. O evento de hoje é igual em qualquer um dos casos; o contador é a
# única coisa na tela que distingue os dois.
#
# Duas honestidades obrigatórias aqui, ambas medidas em produção (2026-09-18):
#
# 1. **O registro de massivas começou em 09/09** — 24 eventos, 9 dias. Um rótulo
#    "N nos últimos 90 dias" sobre 9 dias de histórico é falso por omissão: quem
#    lê "1 massiva em 90 dias" entende trecho saudável, quando o que a base diz
#    é "1 massiva na única semana que observamos". Por isso a janela efetiva é
#    sempre a menor entre os 90 dias e o histórico que existe, e ela vai escrita
#    ao lado do número.
# 2. **16 das 24 massivas são de escopo GEO e não têm elemento** — o cluster
#    geográfico não é cadastro, é um agrupamento que muda de forma a cada
#    evento. Contar reincidência de "GEO" juntaria bairros diferentes num
#    contador só. Sem elemento, o bloco se recusa.
REINCIDENCIA_DIAS = 90


def compute_reincidencia(
    org: Any,
    outages: list[OutageEvent],
    *,
    now: datetime,
    dias: int = REINCIDENCIA_DIAS,
) -> dict[int, dict[str, Any]]:
    """Quantas massivas cada elemento já teve na janela — e desde quando olhamos.

    Devolve um dict por `outage.pk` porque a contagem é uma query só para todas
    as massivas da tela: o histórico é curto (poucas por dia, §1) e trazer a
    janela inteira em memória sai mais barato que um `count()` por card.

    A identidade do trecho é `(scope, element_external_id)` — o par que o
    detector grava. Não é `element_label`, que é texto de tela e muda quando o
    cadastro é corrigido, nem `suspected_segment_label`, que depende de quem
    caiu naquele evento e seria diferente a cada ocorrência do mesmo trecho.
    """
    if not outages:
        return {}

    inicio_janela = now - timedelta(days=dias)
    # De quando é o registro mais antigo da organização — o que limita a janela
    # de verdade. Sem isto, o denominador do contador seria imaginário.
    primeira = (
        OutageEvent.objects.filter(organization=org)
        .order_by("started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    observado_desde = max(primeira, inicio_janela) if primeira else inicio_janela
    dias_observados = max(int((now - observado_desde).total_seconds() // 86400), 0)
    janela_parcial = bool(primeira and primeira > inicio_janela)

    eventos = list(
        OutageEvent.objects.filter(organization=org, started_at__gte=inicio_janela)
        .exclude(element_external_id="")
        .values_list("id", "scope", "element_external_id", "started_at")
    )
    por_elemento: dict[tuple[str, str], list[tuple[int, datetime]]] = {}
    for pk, scope, element_id, inicio in eventos:
        por_elemento.setdefault((scope, element_id), []).append((pk, inicio))

    if janela_parcial:
        plural = "s" if dias_observados != 1 else ""
        ressalva = (
            f"O registro de massivas começa em {observado_desde:%d/%m} — "
            f"{dias_observados} dia{plural} de histórico, menos que a janela de "
            f"{dias} dias. O contador não enxerga antes disso."
        )
        janela_str = f"{dias_observados} dia{plural} de registro"
    else:
        ressalva = ""
        janela_str = f"{dias} dias"

    saida: dict[int, dict[str, Any]] = {}
    for outage in outages:
        base = {
            "janela_dias": dias,
            "janela_str": janela_str,
            "observado_desde": observado_desde,
            "dias_observados": dias_observados,
            "janela_parcial": janela_parcial,
            "ressalva_janela": ressalva,
            "ultima_anterior": None,
        }
        if not outage.element_external_id:
            saida[outage.pk] = {
                **base,
                "determinavel": False,
                "motivo": (
                    f"Agrupamento por {_SCOPE_NOUN.get(outage.scope, outage.scope)} "
                    "não tem elemento fixo no cadastro: o conjunto de clientes muda "
                    "a cada evento, então não há trecho para contar reincidência."
                ),
                "total": 0,
                "anteriores": 0,
                "reincidente": False,
                "frase": "",
            }
            continue

        ocorrencias = por_elemento.get((outage.scope, outage.element_external_id), [])
        # Só o que veio ANTES desta massiva entra no ordinal. Uma encerrada
        # aberta no histórico não pode virar "a 4ª" por causa do que aconteceu
        # depois dela.
        anteriores = [
            inicio
            for pk, inicio in ocorrencias
            if pk != outage.pk and inicio < outage.started_at
        ]
        total = len(anteriores) + 1
        saida[outage.pk] = {
            **base,
            "determinavel": True,
            "motivo": "",
            "total": total,
            "anteriores": len(anteriores),
            "reincidente": bool(anteriores),
            "ultima_anterior": max(anteriores) if anteriores else None,
            "frase": (
                f"{total}ª massiva deste elemento em {janela_str}"
                if anteriores
                else f"Primeira massiva deste elemento em {janela_str}"
            ),
        }
    return saida


# =============================================================================
# Mensagem para o técnico — o que mandar no WhatsApp de quem vai atender
# =============================================================================
# Pedido do operador (21/09/2026): um botão que copia uma mensagem pronta com o
# necessário para o técnico sair de casa sabendo para onde ir — quantos estão
# fora, onde é, o ponto no mapa, a caixa e o cabo que passa ali.
#
# A mensagem é montada no servidor, e não no navegador, por um motivo: ela
# repete afirmações que a tela leva a sério (trecho suspeito é inferência, cabo
# é candidato). Montá-la em JavaScript separaria o texto das regras que o
# justificam, e no dia em que a regra mudasse a mensagem continuaria a antiga.


def _ponto_no_mapa(lat: float, lon: float) -> str:
    """Link do Google Maps — o que o técnico abre no celular.

    Formato de busca por coordenada, não de rota: quem recebe decide se vai
    traçar caminho de onde está, e uma rota pré-montada a partir do POP seria um
    palpite sobre de onde a pessoa sai.
    """
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def compute_mensagem_tecnico(
    org: Any,
    linha: dict[str, Any],
    quedas: list[ConnectionDropEvent],
    *,
    now: datetime | None = None,
) -> str:
    """Texto pronto para colar no WhatsApp do técnico.

    Ordem deliberada: primeiro **o tamanho** (quantos estão fora), depois **onde**
    (caixa e ponto no mapa), depois **o que ajuda a procurar** (cabo, emenda,
    causa provável). É a ordem em que a pergunta aparece na cabeça de quem vai
    atender.

    As ressalvas vêm juntas, curtas: "trecho suspeito" e "cabo candidato" são
    inferência de cadastro, e mandar um técnico com falsa certeza é pior que
    mandá-lo sem informação — ele para de procurar onde deveria.
    """
    now = now or timezone.now()
    partes: list[str] = []

    minutos = int((now - linha["started_at"]).total_seconds() // 60)
    partes.append(f"*MASSIVA* · {linha['ainda_fora']} clientes fora agora")
    partes.append(
        f"{linha['affected_count']} afetados desde {linha['started_at']:%H:%M} "
        f"({minutos} min) · confiança {linha['confidence_label'].lower()}"
    )

    if linha["tem_trecho"]:
        partes.append(f"\nTrecho suspeito: {linha['trecho_suspeito']}")
        partes.append("(inferência pelo cadastro, não leitura do cabo)")
    else:
        partes.append(f"\n{linha['elemento_frase']}")
    if linha.get("referencia"):
        partes.append(linha["referencia"])

    # Caixas afetadas e o ponto no mapa. A coordenada é a da primeira caixa com
    # posição — é o lugar concreto para onde o técnico vai; sem caixa
    # cadastrada, não se inventa ponto.
    ctos_ids = {q.cto_external_id for q in quedas if q.cto_external_id}
    caixas = list(
        NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in=ctos_ids,
        ).only("external_id", "name", "latitude", "longitude")
    )
    nomes = [c.name or c.external_id for c in caixas]
    if nomes:
        partes.append(f"\nCaixas: {', '.join(sorted(nomes)[:6])}")
        if len(nomes) > 6:
            partes.append(f"(+{len(nomes) - 6} caixas)")

    com_posicao = [c for c in caixas if c.latitude is not None and c.longitude is not None]
    if com_posicao:
        alvo = sorted(com_posicao, key=lambda c: c.name or c.external_id)[0]
        partes.append(f"\nMapa: {_ponto_no_mapa(float(alvo.latitude), float(alvo.longitude))}")
        partes.append(f"({alvo.name or alvo.external_id} · {alvo.latitude:.6f}, {alvo.longitude:.6f})")

    cabos = (linha.get("cabos") or {}).get("cabos") or []
    if cabos:
        principal = cabos[0]
        classe = f" ({principal['classe'].lower()})" if principal["classe"] else ""
        partes.append(
            f"\nCabo candidato: {principal['nome']}{classe} — "
            f"passa a {principal['distancia_m']:.0f} m de {principal['ctos_tocadas']} "
            f"caixa{'s' if principal['ctos_tocadas'] != 1 else ''} do evento"
        )

    veredito = linha.get("veredito") or {}
    if veredito.get("rotulo"):
        partes.append(f"\nCausa provável: {veredito['rotulo']}")
        if veredito.get("base"):
            partes.append(f"({veredito['base']})")

    if linha.get("is_expected"):
        partes.append("\n⚠ Dentro de manutenção programada.")

    return "\n".join(partes)


# =============================================================================
# Cabos candidatos (R5) — destravado pela geometria (spike R2)
# =============================================================================
# Até 2026-09-19 esta pergunta não tinha resposta possível: nenhum campo liga
# cabo a CTO, e "os 562 cabos do projeto" seria ruído. A geometria destravou o
# vínculo por proximidade — 1.120 das 1.431 caixas estão a ≤10 m de um cabo, com
# mediana 0,0 m.
#
# O que a tela pode dizer: "estes cabos passam pelas caixas que caíram". O que
# ela não pode: que a fibra passa exatamente ali, que é esse cabo que alimenta a
# caixa, ou que ele rompeu. Candidato é candidato.

# Quantos cabos cabem no card antes de virar lista. Acima disto a informação
# vira ruído — e uma massiva grande toca muitos cabos de atendimento: em
# produção, uma massiva de escopo OLT com 15 caixas tocou 24 cabos.
_MAX_CABOS_NO_CARD = 6


def tracados_de_cabo(org: Any) -> list[Any]:
    """Todos os traçados de cabo da organização, prontos para o domínio.

    Fica separado porque a tela geral tem várias massivas abertas e o conjunto
    de cabos é o mesmo para todas: carregá-lo uma vez por massiva seria reler
    1.191 polilinhas do banco a cada card, a cada 3 min do auto-refresh.
    """
    from apps.network.domain.geometry import PathInput

    return [
        PathInput(
            external_id=g.external_id,
            name=g.name,
            points=[(float(lat), float(lon)) for lat, lon in g.points],
            project_external_id=g.project_external_id,
            type_name=g.type_name,
        )
        for g in NetworkElementGeometry.objects.filter(
            organization=org, kind=NetworkElement.Kind.CABLE
        ).only("external_id", "name", "points", "project_external_id", "type_name")
        if len(g.points) >= 2
    ]


def compute_cabos_candidatos(
    org: Any,
    quedas: list[ConnectionDropEvent],
    *,
    tracados: list[Any] | None = None,
) -> dict[str, Any]:
    """Cabos cadastrados que passam perto das caixas afetadas.

    Devolve `determinavel=False` quando não há caixa com coordenada ou não há
    traçado sincronizado — dois estados diferentes, com motivos diferentes, e
    nenhum deles é "nenhum cabo candidato".

    `tracados` vem de fora quando a tela tem mais de uma massiva: é o mesmo
    conjunto de cabos para todas.
    """
    from apps.network.domain.geometry import (
        RAIO_CANDIDATO_METROS,
        candidate_cables,
        ctos_sem_cabo,
    )

    ctos_afetadas = {q.cto_external_id for q in quedas if q.cto_external_id}
    pontos = [
        (float(e.latitude), float(e.longitude))
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in=ctos_afetadas,
            latitude__isnull=False,
            longitude__isnull=False,
        ).only("latitude", "longitude")
    ]
    if not pontos:
        return {
            "determinavel": False,
            "motivo": (
                "Nenhuma caixa desta massiva tem coordenada no cadastro, então "
                "não há de onde medir a proximidade de um cabo."
            ),
            "cabos": [],
            "raio_m": int(RAIO_CANDIDATO_METROS),
        }

    if tracados is None:
        tracados = tracados_de_cabo(org)
    if not tracados:
        return {
            "determinavel": False,
            "motivo": (
                "O traçado dos cabos ainda não foi sincronizado nesta "
                "organização — sem ele não há candidato a apontar."
            ),
            "cabos": [],
            "raio_m": int(RAIO_CANDIDATO_METROS),
        }

    candidatos = candidate_cables(pontos, tracados)
    sem_cabo = ctos_sem_cabo(pontos, tracados)
    return {
        "determinavel": True,
        "motivo": "",
        "cabos": [
            {
                "external_id": c.external_id,
                "nome": c.name,
                "classe": c.classe,
                "distancia_m": c.distance_meters,
                "ctos_tocadas": c.ctos_tocadas,
            }
            for c in candidatos[:_MAX_CABOS_NO_CARD]
        ],
        "total": len(candidatos),
        "alem_do_card": max(len(candidatos) - _MAX_CABOS_NO_CARD, 0),
        "raio_m": int(RAIO_CANDIDATO_METROS),
        # Quantos dos cabos listados ficam sem classe. *Medido em produção
        # (2026-09-19):* lendo o tipo do cadastro, 99 dos 1.191 cabos — 8% —
        # não se classificam (tipos "FIBRA AS80 24FO", "144FO", e 11 sem tipo).
        # Sem isso escrito, a ausência do selo pareceria afirmação de que o cabo
        # não é tronco.
        "sem_classe": sum(1 for c in candidatos[:_MAX_CABOS_NO_CARD] if not c.classe),
        "ctos_com_coordenada": len(pontos),
        # Caixa do evento sem cabo cadastrado por perto. Declarado porque uma
        # lista curta de candidatos tem dois significados opostos: "achamos
        # pouco" e "metade das caixas não tem cabo cadastrado".
        "ctos_sem_cabo": sem_cabo,
        # Os ids que o mapa desenha — só os candidatos. O mapa com os 1.191
        # cabos do cadastro seria um borrão.
        "ids_no_mapa": [c.external_id for c in candidatos[:_MAX_CABOS_NO_CARD]],
    }


# Raio para mostrar uma caixa de emenda no mapa. Maior que o dos cabos (30 m)
# porque a emenda não precisa encostar na caixa afetada para explicá-la: ela
# fica no poste da esquina, no meio do trecho. Medido contra a realidade do
# cadastro: 150 m é a distância em que a emenda ainda é "aquela ali" para quem
# está na rua.
RAIO_EMENDA_METROS = 150.0


def emendas_proximas(org: Any, quedas: list[ConnectionDropEvent]) -> list[dict[str, Any]]:
    """Caixas de emenda perto das caixas afetadas — onde o cabo é aberto.

    A emenda é o primeiro lugar que o técnico abre quando o trecho passa por
    ali: é onde a fibra foi cortada e refeita, e onde uma fusão mal feita
    aparece meses depois. São 317 no cadastro; mostrar todas seria um mapa de
    pontos sem pergunta, então só entram as que estão perto do evento.
    """
    from apps.network.domain.geometry import distance_to_path

    pontos_afetados = [
        (float(e.latitude), float(e.longitude))
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in={q.cto_external_id for q in quedas if q.cto_external_id},
            latitude__isnull=False,
            longitude__isnull=False,
        ).only("latitude", "longitude")
    ]
    if not pontos_afetados:
        return []

    saida = []
    for emenda in NetworkElementGeometry.objects.filter(
        organization=org, kind=NetworkElement.Kind.SPLICE
    ).only("external_id", "name", "points"):
        if not emenda.points:
            continue
        ponto = (float(emenda.points[0][0]), float(emenda.points[0][1]))
        distancia = min(distance_to_path(p, [ponto]) for p in pontos_afetados)
        if distancia > RAIO_EMENDA_METROS:
            continue
        saida.append({
            "lat": ponto[0],
            "lon": ponto[1],
            "label": f"{emenda.name or emenda.external_id} · emenda a {distancia:.0f} m",
            "distancia_m": round(distancia),
        })
    saida.sort(key=lambda e: e["distancia_m"])
    return saida


def _tracados_do_mapa(org: Any, ids: list[str]) -> list[dict[str, Any]]:
    """Polilinhas dos cabos candidatos, prontas para o Plotly."""
    if not ids:
        return []
    return [
        {
            "nome": g.name or g.external_id,
            "pontos": [[float(lat), float(lon)] for lat, lon in g.points],
        }
        for g in NetworkElementGeometry.objects.filter(
            organization=org, kind=NetworkElement.Kind.CABLE, external_id__in=ids
        ).only("external_id", "name", "points")
        if len(g.points) >= 2
    ]


def compute_mapa(
    org: Any,
    quedas: list[ConnectionDropEvent],
    *,
    vizinhas_intactas: list[dict[str, Any]] | None = None,
    cabos_candidatos: list[str] | None = None,
) -> dict[str, Any]:
    """Pontos e ligações do mapa: quem está fora, quem voltou, CTOs, POPs.

    A separação fora/voltou é o coração da tela: durante o reparo a equipe
    precisa ver o vermelho virando verde. O carimbo de hora vai no rótulo do
    ponto (caiu às / voltou às) — sem ele, "verde" não diria *quando* voltou.

    As **ligações** (R3) são retas entre dois pontos do cadastro — caixa → POP e
    caixa a montante → demais afetadas — e **não são o caminho da fibra**. Por
    isso saem tracejadas: linha cheia leria como traçado, e o técnico cavaria
    onde a linha passa.

    O **traçado do cabo** (G5) é a exceção que agora existe: aquilo é geometria
    de verdade, vinda do InMap, e sai em linha cheia. Só dos cabos candidatos —
    desenhar os 1.191 do cadastro daria um borrão sem pergunta.

    `vizinhas_intactas` (R6) são as caixas da mesma PON que não caíram. Entram
    como ponto vazado: é o que delimita o trecho.
    """
    com_posicao = [
        q for q in quedas if q.latitude is not None and q.longitude is not None
    ]
    clientes = [
        {
            "lat": q.latitude,
            "lon": q.longitude,
            "label": f"{q.login or 'login'} · caiu {q.dropped_at:%H:%M}",
        }
        for q in com_posicao
        if q.restored_at is None
    ]
    voltaram = [
        {
            "lat": q.latitude,
            "lon": q.longitude,
            "label": f"{q.login or 'login'} · voltou {q.restored_at:%H:%M}",
        }
        for q in com_posicao
        if q.restored_at is not None
    ]

    ctos_afetadas = {q.cto_external_id for q in quedas if q.cto_external_id}
    ctos = [
        {"lat": e.latitude, "lon": e.longitude, "label": e.name or e.external_id}
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in=ctos_afetadas,
            latitude__isnull=False,
            longitude__isnull=False,
        )
    ]
    pops = [
        {"lat": e.latitude, "lon": e.longitude, "label": e.name or e.external_id}
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.POP,
            latitude__isnull=False,
            longitude__isnull=False,
        )
    ]

    vizinhas = [
        {"lat": v["lat"], "lon": v["lon"], "label": f"{v['nome']} · {v['de_pe']} no ar"}
        for v in (vizinhas_intactas or [])
        if v.get("lat") is not None and v.get("lon") is not None
    ]

    ligacoes, trecho = _ligacoes_do_mapa(org, ctos_afetadas)
    # O trecho suspeito sobre o CABO (e não a reta entre caixas), quando existe
    # um cabo candidato que passa pelas duas pontas.
    trecho_no_cabo = _trecho_sobre_o_cabo(org, trecho, cabos_candidatos or [])

    # Quantos clientes fora ficaram FORA do mapa. Sem isso, um mapa com 3 pontos
    # sobre 40 quedas seria lido como "a massiva é pequena".
    sem_coordenada = len(quedas) - len(com_posicao)
    return {
        "clientes": clientes,
        "voltaram": voltaram,
        "ctos": ctos,
        "pops": pops,
        "vizinhas": vizinhas,
        "ligacoes": ligacoes,
        "trecho": trecho,
        "cabos": _tracados_do_mapa(org, cabos_candidatos or []),
        "trecho_no_cabo": trecho_no_cabo,
        "emendas": emendas_proximas(org, quedas),
        "sem_coordenada": sem_coordenada,
        "total_quedas": len(quedas),
    }


def _trecho_sobre_o_cabo(
    org: Any, trecho: list[dict[str, Any]], cabos_candidatos: list[str]
) -> list[dict[str, Any]]:
    """O trecho suspeito desenhado **sobre o cabo**, quando há cabo que sirva.

    A reta entre duas caixas atravessa quarteirão; o cabo faz a curva da rua. Com
    a geometria no banco, dá para dizer "o trecho é este pedaço do cabo X" — que
    é o que o técnico precisa para saber por onde andar.

    Só sai quando um cabo candidato passa pelas **duas** pontas do trecho. Se
    nenhum passar, a reta tracejada continua valendo e o mapa segue dizendo o
    que sempre disse: ligação lógica, não caminho da fibra.
    """
    from apps.network.domain.geometry import sub_path_between

    if not trecho or not cabos_candidatos:
        return []

    tracados = [
        (g.name or g.external_id, [(float(lat), float(lon)) for lat, lon in g.points])
        for g in NetworkElementGeometry.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CABLE,
            external_id__in=cabos_candidatos,
        ).only("external_id", "name", "points")
        if len(g.points) >= 2
    ]

    saida: list[dict[str, Any]] = []
    for segmento in trecho:
        for nome, pontos in tracados:
            pedaco = sub_path_between(pontos, segmento["de"], segmento["para"])
            if not pedaco:
                continue
            saida.append({
                "nome": f"{segmento['label']} · pelo {nome}",
                "pontos": [[lat, lon] for lat, lon in pedaco],
            })
            # Um cabo por trecho: o primeiro que serve às duas pontas é o
            # candidato, e empilhar todos os que passam por ali transformaria a
            # pista em rabisco.
            break
    return saida


def _ligacoes_do_mapa(
    org: Any, ctos_afetadas: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Segmentos CTO→POP e o trecho entre as caixas afetadas.

    A CTO aponta para a OLT (100% do cadastro) e a OLT aponta para o POP — a OLT
    não tem coordenada própria em nenhuma das três, então a ponta de cima da
    ligação é a coordenada do POP.

    O sentido do trecho usa a mesma distância do detector (`haversine_meters`):
    a caixa mais próxima do POP é a de montante. Se a tela desenhasse a seta em
    uma direção e o rótulo do trecho dissesse outra, uma das duas estaria
    mentindo.
    """
    from apps.network.domain.outage import haversine_meters

    if not ctos_afetadas:
        return [], []

    ctos = {
        e.external_id: e
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in=ctos_afetadas,
            latitude__isnull=False,
            longitude__isnull=False,
        )
    }
    if not ctos:
        return [], []

    olts = {
        e.external_id: e.parent_external_id
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.OLT,
            external_id__in={c.parent_external_id for c in ctos.values() if c.parent_external_id},
        )
    }
    pops = {
        e.external_id: (e.latitude, e.longitude, e.name or e.external_id)
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.POP,
            external_id__in={p for p in olts.values() if p},
            latitude__isnull=False,
            longitude__isnull=False,
        )
    }

    ligacoes: list[dict[str, Any]] = []
    pop_de: dict[str, tuple[float, float]] = {}
    for cto_id, elemento in ctos.items():
        pop_id = olts.get(elemento.parent_external_id, "")
        pop = pops.get(pop_id)
        if pop is None:
            continue
        pop_de[cto_id] = (pop[0], pop[1])
        ligacoes.append({
            "de": (elemento.latitude, elemento.longitude),
            "para": (pop[0], pop[1]),
            "label": f"{elemento.name or cto_id} → {pop[2]}",
        })

    # Trecho: uma CADEIA de caixa vizinha em caixa vizinha, começando na mais
    # próxima do POP. Com uma caixa só não há trecho — e sem POP não há como
    # saber quem está a montante, então o desenho fica de fora em vez de chutar
    # um sentido.
    #
    # Era uma estrela (a caixa de montante ligada a cada uma das outras) até
    # 19/09/2026. A estrela desenhava pares distantes por construção: *medido em
    # produção*, as pontas ficavam a 11 km uma da outra na mediana, enquanto os
    # cabos do cadastro têm mediana de 201 m. Além de atravessar a cidade em
    # linha reta, isso impedia o trecho de seguir o cabo — nenhum cabo cobre 11
    # km. Em cadeia, cada par é o pulo real de uma caixa para a seguinte.
    #
    # O vizinho é o mais próximo ainda não visitado. É heurística de desenho, não
    # de topologia: a ordem real da rede não está no cadastro, e a cadeia serve
    # para mostrar por onde o problema se espalha, não para afirmar a sequência
    # de alimentação.
    trecho: list[dict[str, Any]] = []
    com_pop = [c for c in ctos if c in pop_de]
    if len(ctos) >= 2 and com_pop:
        referencia = pop_de[sorted(com_pop)[0]]
        atual = min(
            sorted(ctos),
            key=lambda c: haversine_meters(referencia, (ctos[c].latitude, ctos[c].longitude)),
        )
        restantes = set(ctos) - {atual}
        while restantes:
            origem = ctos[atual]
            ponto_origem = (origem.latitude, origem.longitude)
            proximo = min(
                sorted(restantes),
                key=lambda c: haversine_meters(
                    ponto_origem, (ctos[c].latitude, ctos[c].longitude)
                ),
            )
            destino = ctos[proximo]
            trecho.append({
                "de": ponto_origem,
                "para": (destino.latitude, destino.longitude),
                "label": f"{origem.name or atual} → {destino.name or proximo}",
            })
            restantes.discard(proximo)
            atual = proximo
    return ligacoes, trecho


def compute_timeline(org: Any, *, now: datetime) -> list[dict[str, Any]]:
    """Quedas por bucket de 10 min nas últimas horas.

    Separa "ainda fora" de "já voltou" porque durante um reparo é essa diferença
    que a equipe olha — uma barra que encolhe é gente voltando.
    """
    inicio = now - timedelta(hours=TIMELINE_HOURS)
    quedas = ConnectionDropEvent.objects.filter(
        organization=org, dropped_at__gte=inicio
    ).only("dropped_at", "restored_at")

    passo = timedelta(minutes=BUCKET_MINUTES)
    inicio_bucket = _floor_bucket(inicio)
    buckets: dict[datetime, dict[str, int]] = {}
    t = inicio_bucket
    while t <= now:
        buckets[t] = {"fora": 0, "voltou": 0}
        t += passo

    for q in quedas:
        chave = _floor_bucket(q.dropped_at)
        alvo = buckets.get(chave)
        if alvo is None:
            continue
        alvo["voltou" if q.restored_at else "fora"] += 1

    return [
        {
            "inicio": momento,
            "label": momento.strftime("%H:%M"),
            "fora": v["fora"],
            "voltou": v["voltou"],
        }
        for momento, v in sorted(buckets.items())
    ]


def _floor_bucket(momento: datetime) -> datetime:
    minuto = (momento.minute // BUCKET_MINUTES) * BUCKET_MINUTES
    return momento.replace(minute=minuto, second=0, microsecond=0)


# =============================================================================
# Detalhe de uma massiva
# =============================================================================
def _signal_fields_available() -> bool:
    """As colunas de sinal óptico da #148 já existem no modelo?

    Consultado em tempo de chamada (não no import) porque as duas issues correm
    em paralelo: quando a #148 entrar, a tabela ganha as colunas sem que nada
    aqui precise mudar.
    """
    nomes = {f.name for f in ConnectionDropEvent._meta.get_fields()}
    return all(n in nomes for n in _SIGNAL_FIELD_NAMES)


def _dbm_or_none(valor: Any) -> float | None:
    """Leitura válida em dBm, ou None.

    `0.00` conta como ausência, não como zero dBm: é assim que o IXC representa
    "a ONU não reportou" (1.391 de 4.554 registros na medição de §2.8). Tratar o
    zero como leitura faria todo cliente caído aparecer com ~24 dB de perda.
    """
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return None if numero == 0.0 else numero


def _fmt_dbm(valor: Any) -> str:
    """'-24,43 dBm' — e **nunca** '0,00 dBm' pra ausência de leitura (§2.8)."""
    numero = _dbm_or_none(valor)
    if numero is None:
        return "sem leitura"
    return f"{numero:.2f}".replace(".", ",") + " dBm"


def _signal_cell(drop: ConnectionDropEvent | None) -> dict[str, Any]:
    """Sinal antes/depois de uma queda, renderizado defensivamente.

    Regras de §2.8 que valem mesmo com a coluna ausente:
    - nulo/ausente é "sem leitura", nunca 0,00 dBm (0,00 no IXC é ausência de
      leitura, e exibi-lo faria todo cliente caído parecer 24 dB pior);
    - cada leitura vem com o carimbo de tempo, porque a base costuma ser da
      varredura das ~06:30 e pode ter 18h;
    - retorno pior que a base além de 3 dB é marcado — cheiro de fusão mal feita.
    """
    disponivel = _signal_fields_available()
    antes = _dbm_or_none(getattr(drop, "signal_rx_before", None) if drop else None)
    depois = _dbm_or_none(getattr(drop, "signal_rx_after", None) if drop else None)
    # Carimbo só acompanha leitura de verdade: "sem leitura, medido 06:30" seria
    # ruído; "-24,43 dBm, medido 06:30" é o que revela que a base tem 18h.
    antes_em = (
        getattr(drop, "signal_before_measured_at", None)
        if drop and antes is not None
        else None
    )
    depois_em = (
        getattr(drop, "signal_after_measured_at", None)
        if drop and depois is not None
        else None
    )

    delta = None
    degradado = False
    if antes is not None and depois is not None:
        # dBm é negativo: retorno MENOR que a base é sinal pior.
        delta = float(depois) - float(antes)
        degradado = delta < -SIGNAL_DEGRADATION_DB

    return {
        "campos_disponiveis": disponivel,
        "antes_str": _fmt_dbm(antes),
        "antes_em": antes_em,
        "depois_str": _fmt_dbm(depois),
        "depois_em": depois_em,
        "delta_str": (
            f"{delta:+.1f} dB".replace(".", ",") if delta is not None else ""
        ),
        "degradado": degradado,
    }


def _cabecalho_com_mensagem(
    org: Any,
    outage: OutageEvent,
    quedas: list[ConnectionDropEvent],
    *,
    now: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """`outage_row` + a mensagem do técnico, que depende da linha já montada."""
    linha = outage_row(outage, **kwargs)
    linha["mensagem_tecnico"] = compute_mensagem_tecnico(org, linha, quedas, now=now)
    return linha


def compute_massiva_detalhe(
    org: Any, outage: OutageEvent, *, now: datetime | None = None
) -> dict[str, Any]:
    """Cabeçalho + tabela de clientes de uma massiva."""
    afetados = list(
        OutageAffectedLogin.objects.filter(organization=org, outage=outage)
        .select_related("drop_event", "drop_event__customer", "drop_event__connection")
        .order_by("dropped_at")
    )

    contratos_ids = {
        a.drop_event.connection.contract_external_id
        for a in afetados
        if a.drop_event and a.drop_event.connection
        and a.drop_event.connection.contract_external_id
    }
    planos = {
        c.external_id: c
        for c in Contract.objects.filter(
            organization=org, external_id__in=contratos_ids
        ).only("external_id", "plan_name")
    }

    ctos_ids = {
        a.drop_event.cto_external_id
        for a in afetados
        if a.drop_event and a.drop_event.cto_external_id
    }
    nomes_cto = {
        e.external_id: (e.name or e.external_id)
        for e in NetworkElement.objects.filter(
            organization=org,
            kind=NetworkElement.Kind.CTO,
            external_id__in=ctos_ids,
        ).only("external_id", "name")
    }

    linhas = []
    com_leitura_base = 0
    for a in afetados:
        drop = a.drop_event
        conexao = drop.connection if drop else None
        cliente = drop.customer if drop else None
        contrato_id = conexao.contract_external_id if conexao else ""
        contrato = planos.get(contrato_id)
        cto_id = drop.cto_external_id if drop else ""
        sinal = _signal_cell(drop)
        if sinal["antes_str"] != "sem leitura":
            com_leitura_base += 1

        linhas.append({
            "nome": (cliente.name if cliente else "") or a.login or "—",
            "contrato": contrato_id or "—",
            "plano": contrato.plan_name if contrato else "—",
            "telefone": (cliente.phone if cliente else "") or "—",
            "cto": nomes_cto.get(cto_id, cto_id) or "—",
            "porta": (drop.cto_port if drop else "") or "—",
            "dropped_at": a.dropped_at,
            "restored_at": a.restored_at,
            "voltou": a.restored_at is not None,
            "sinal": sinal,
        })

    quedas = [a.drop_event for a in afetados if a.drop_event]
    cabos = compute_cabos_candidatos(org, quedas)
    return {
        "causas_onu": compute_causas_onu(quedas),
        "motivos": compute_motivos(quedas),
        "cabecalho": _cabecalho_com_mensagem(
            org,
            outage,
            quedas,
            now=now or timezone.now(),
            referencia=element_references(org, [outage]).get(outage.pk, ""),
            veredito=compute_veredito(quedas, scope=outage.scope),
            vizinhanca=compute_vizinhanca(org, quedas),
            reincidencia=compute_reincidencia(
                org, [outage], now=now or timezone.now()
            ).get(outage.pk),
            cabos=cabos,
        ),
        "linhas": linhas,
        "sinal_disponivel": _signal_fields_available(),
        "sinal_cobertura": {
            "com_base": com_leitura_base,
            "total": len(linhas),
        },
        "mapa": compute_mapa(
            org,
            quedas,
            vizinhas_intactas=compute_vizinhanca(org, quedas).get("intactas", []),
            cabos_candidatos=cabos.get("ids_no_mapa", []),
        ),
    }


# =============================================================================
# Causa confirmada (R9) — a primeira entrada de dados desta ferramenta
# =============================================================================
# O veredito automático (R7) diz "provável energia" e nunca fica sabendo se
# acertou. A reincidência (R8) conta eventos sem saber o que eram. É este campo,
# preenchido por gente depois que a massiva encerra, que fecha o ciclo — e que
# vira rótulo de modelo quando houver passado suficiente.
#
# Vocabulário fechado (decidido com o operador em 19/09/2026): causa exclusiva,
# porque é ela que o modelo vai prever, mais tags livres da lista abaixo, que
# carregam o contexto que a causa não cabe. Se tudo fosse tag, nada seria a
# resposta.

CAUSE_TAGS: tuple[tuple[str, str], ...] = (
    ("vandalismo", "vandalismo"),
    ("troca_de_poste", "troca de poste"),
    ("terceiro", "terceiro (obra, acidente)"),
    ("concessionaria", "concessionária"),
    ("energia_propria", "energia própria/nobreak"),
    ("chuva", "chuva"),
)
_CAUSE_TAG_KEYS = {chave for chave, _ in CAUSE_TAGS}

# Quantas massivas sem causa a fila mostra. Fila longa não é cobrança, é
# paisagem: some da vista e deixa de ser feita.
_MAX_NA_FILA = 8


def tag_labels(tags: list[str] | None) -> list[str]:
    """Nomes legíveis das tags gravadas, ignorando o que saiu do vocabulário."""
    nomes = dict(CAUSE_TAGS)
    return [nomes[t] for t in (tags or []) if t in nomes]


def normalize_tags(valores: list[str]) -> list[str]:
    """Filtra o que veio do formulário contra o vocabulário, preservando a ordem.

    Tag fora da lista é descartada em silêncio: o campo é rótulo de treino, e
    aceitar texto livre aqui encheria o vocabulário de sinônimos ("vandalismo",
    "vandalizado", "roubo de cabo") que nenhum modelo consegue juntar depois.
    """
    return [chave for chave, _ in CAUSE_TAGS if chave in set(valores)]


def base_de_clientes(org: Any) -> int:
    """Quantos logins ativos a operação tem — o denominador do "% da base".

    Mesma regra do denominador do detector: contrato cancelado deixa login para
    trás no ERP e inflaria a base, fazendo toda massiva parecer pequena.
    """
    from apps.customers.infrastructure.models import Contract

    inativos = set(
        Contract.objects.filter(organization=org)
        .exclude(status=Contract.Status.ACTIVE)
        .values_list("external_id", flat=True)
    )
    total = 0
    for contrato_id in Connection.objects.filter(organization=org).values_list(
        "contract_external_id", flat=True
    ):
        if contrato_id and contrato_id in inativos:
            continue
        total += 1
    return total


def compute_sem_causa(org: Any, *, limit: int = _MAX_NA_FILA) -> dict[str, Any]:
    """Massivas encerradas esperando alguém dizer o que elas eram.

    Fica no topo da aba porque cobrança que não aparece não é cobrança. E conta
    o total, não só as que cabem na tela: "8 na fila" e "8 de 40" pedem ações
    diferentes.
    """
    pendentes = OutageEvent.objects.filter(
        organization=org,
        ended_at__isnull=False,
        confirmed_cause="",
        # Dispensada sai da fila sem ter sido respondida — ver
        # `cause_waived_at`. São estados diferentes, e juntá-los aqui traria de
        # volta o que alguém decidiu descartar.
        cause_waived_at__isnull=True,
    ).order_by("-ended_at")
    total = pendentes.count()
    eventos = list(pendentes[:limit])
    referencias = element_references(org, eventos)
    linhas = [
        outage_row(o, referencia=referencias.get(o.pk, ""))
        for o in eventos
    ]
    return {
        "linhas": linhas,
        "total": total,
        "alem_da_lista": max(total - len(linhas), 0),
        "causas": OutageEvent.Cause.choices,
        "tags": CAUSE_TAGS,
    }


def compute_placar_do_veredito(org: Any) -> dict[str, Any]:
    """Quanto o veredito automático acerta, medido contra a causa confirmada.

    É o número que justifica ter pedido a alguém para preencher o campo. Ele só
    existe onde as duas coisas existem: massiva com causa confirmada E veredito
    que não se recusou a opinar.

    Enquanto a amostra for pequena, a tela diz o tamanho dela em vez de exibir
    uma porcentagem que oscila 20 pontos a cada linha nova.
    """
    confirmadas = list(
        OutageEvent.objects.filter(organization=org)
        .exclude(confirmed_cause="")
        .only("id", "scope", "confirmed_cause")
    )
    if not confirmadas:
        return {"tem_amostra": False, "total": 0}

    quedas_por_massiva: dict[int, list[ConnectionDropEvent]] = {}
    for afetado in (
        OutageAffectedLogin.objects.filter(
            organization=org, outage__in=confirmadas
        )
        .select_related("drop_event", "drop_event__connection")
        .only(
            "outage",
            "drop_event",
            "drop_event__connection",
            "drop_event__connection__onu_last_drop_cause",
            "drop_event__dropped_at",
        )
    ):
        if afetado.drop_event is not None:
            quedas_por_massiva.setdefault(afetado.outage_id, []).append(afetado.drop_event)

    # O veredito só nomeia energia ou fibra; as outras causas confirmadas não
    # são comparáveis e ficam de fora da conta em vez de contar como erro.
    comparavel = {
        OutageEvent.Cause.ENERGIA.value: "energia",
        OutageEvent.Cause.ROMPIMENTO.value: "fibra",
    }
    acertos = 0
    comparadas = 0
    opinou_e_nao_deu_pra_comparar = 0
    for outage in confirmadas:
        veredito = compute_veredito(
            quedas_por_massiva.get(outage.pk, []), scope=outage.scope
        )
        palpite = veredito.get("veredito", "")
        if palpite not in ("energia", "fibra"):
            continue
        esperado = comparavel.get(outage.confirmed_cause)
        if esperado is None:
            opinou_e_nao_deu_pra_comparar += 1
            continue
        comparadas += 1
        if palpite == esperado:
            acertos += 1

    return {
        "tem_amostra": comparadas > 0,
        "total": len(confirmadas),
        "comparadas": comparadas,
        "acertos": acertos,
        "pct": round(acertos * 100 / comparadas) if comparadas else 0,
        "fora_da_comparacao": opinou_e_nao_deu_pra_comparar,
        # Abaixo disto a porcentagem oscila demais para ser lida como taxa.
        "amostra_pequena": comparadas < 10,
    }


# =============================================================================
# Histórico (o agregado, e só ele — §1)
# =============================================================================
def compute_historico(
    org: Any, *, limit: int = 20, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Massivas já encerradas — quando, quantas pessoas, onde, genericamente.

    Deliberadamente sem recorte, filtro ou série: a ferramenta é de tempo real e
    não responde pergunta sobre o passado (§1 do plano). O que sobrevive é o
    registro do evento, não analytics dele.
    """
    encerradas = list(
        OutageEvent.objects.filter(organization=org, ended_at__isnull=False).order_by(
            "-ended_at"
        )[:limit]
    )
    referencias = element_references(org, encerradas)
    # A reincidência (R8) é a única leitura do passado que o histórico ganha — e
    # é dela que a tabela vive: a mesma OLT aparecendo quatro vezes só vira
    # informação quando a quarta linha diz que é a quarta.
    reincidencias = compute_reincidencia(org, encerradas, now=now or timezone.now())
    return [
        outage_row(
            o,
            referencia=referencias.get(o.pk, ""),
            reincidencia=reincidencias.get(o.pk),
        )
        for o in encerradas
    ]
