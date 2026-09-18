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

from apps.customers.infrastructure.models import Contract
from apps.network.infrastructure.models import (
    ConnectionDropEvent,
    ConnectionPollState,
    NetworkElement,
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
) -> dict[str, Any]:
    """Uma massiva pronta pro template, com escopo e fração já costurados.

    O domínio guarda `element_label` e `affected_fraction` separados de propósito
    (docstring de `OutageEvent`); é aqui que eles voltam a andar juntos, porque
    é aqui que se sabe que a tela tem espaço pra frase inteira.

    `referencia` é o lugar humano do elemento em escopo ("no POP Portal do
    Pirapora"), derivado do cadastro por `element_references`. Vem de fora
    porque a derivação é uma query e esta função é chamada em laço.

    `veredito` (R7) também vem de fora, e pelo mesmo motivo: depende das quedas
    da massiva, que são outra query. Ausente, a linha sai como sempre saiu.
    """
    escopo_nome = _SCOPE_NOUN.get(outage.scope, outage.scope)
    fracao = outage.affected_fraction or 0.0
    fracao_str = _fracao_str(fracao)

    elemento = outage.element_label or "elemento não identificado"
    # A frase é sempre "quem" + "quanto daquilo". Nunca só "quem".
    elemento_frase = (
        f"{elemento} — {fracao_str} dos logins da {escopo_nome}"
        if outage.element_label
        else f"Sem elemento em escopo — agrupamento por {escopo_nome}"
    )

    trecho = outage.suspected_segment_label or ""
    ressalva = ""
    if outage.scope in _SCOPES_QUE_AGREGAM and fracao < _FRACAO_QUE_EXIGE_RESSALVA:
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
            # A causa da ONU mora na Connection e é o insumo do veredito (R7).
            "drop_event__connection",
            "drop_event__connection__onu_last_drop_cause",
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
    linhas = [
        outage_row(
            o,
            referencia=referencias.get(o.pk, ""),
            veredito=compute_veredito(quedas_por_massiva.get(o.pk, []), scope=o.scope),
        )
        for o in abertas
    ]

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
        "mapa": compute_mapa(org, quedas_no_mapa),
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


def compute_mapa(org: Any, quedas: list[ConnectionDropEvent]) -> dict[str, Any]:
    """Pontos do mapa: quem está fora, quem já voltou, CTOs afetadas e POPs.

    A separação fora/voltou é o coração da tela: durante o reparo a equipe
    precisa ver o vermelho virando verde. O carimbo de hora vai no rótulo do
    ponto (caiu às / voltou às) — sem ele, "verde" não diria *quando* voltou.
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

    # Quantos clientes fora ficaram FORA do mapa. Sem isso, um mapa com 3 pontos
    # sobre 40 quedas seria lido como "a massiva é pequena".
    sem_coordenada = len(quedas) - len(com_posicao)
    return {
        "clientes": clientes,
        "voltaram": voltaram,
        "ctos": ctos,
        "pops": pops,
        "sem_coordenada": sem_coordenada,
        "total_quedas": len(quedas),
    }


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


def compute_massiva_detalhe(org: Any, outage: OutageEvent) -> dict[str, Any]:
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
    return {
        "causas_onu": compute_causas_onu(quedas),
        "motivos": compute_motivos(quedas),
        "cabecalho": outage_row(
            outage,
            referencia=element_references(org, [outage]).get(outage.pk, ""),
            veredito=compute_veredito(quedas, scope=outage.scope),
        ),
        "linhas": linhas,
        "sinal_disponivel": _signal_fields_available(),
        "sinal_cobertura": {
            "com_base": com_leitura_base,
            "total": len(linhas),
        },
        "mapa": compute_mapa(
            org,
            [a.drop_event for a in afetados if a.drop_event],
        ),
    }


# =============================================================================
# Histórico (o agregado, e só ele — §1)
# =============================================================================
def compute_historico(org: Any, *, limit: int = 20) -> list[dict[str, Any]]:
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
    return [outage_row(o, referencia=referencias.get(o.pk, "")) for o in encerradas]
