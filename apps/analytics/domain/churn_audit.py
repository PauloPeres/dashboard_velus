"""Auditoria semântica de churn — o motivo cadastrado confrontado com a observação.

Origem: a auditoria que o Felipe montou à mão em set/2026 sobre os 514
cancelamentos de mar–ago/2026 (export do IXC + reclassificação registro a
registro). Aqui ela vira regra explícita, para rodar sobre a base viva em vez de
uma planilha congelada. As categorias, os níveis de controle e as justificativas
são os dele; o que mudou foi só o mecanismo — regex sobre a observação, em vez
de leitura manual.

Duas perguntas por cancelamento:

1. **Qual foi a causa real?** O motivo que o atendente escolhe no IXC é um
   menu; a observação é a história. "Desconexão por opção" com "se mudou para
   onde não temos cobertura" na observação é mudança, não opção — e essa
   divergência vira a coluna "inconsistência".
2. **Dava para reter?** Cada causa cai num de cinco níveis de controle, do
   "deveria ter retido" (falha operacional com evidência, como reincidência
   técnica) ao "fora do churn" (titularidade e saneamento de base, que não são
   perda de cliente).

E a **competência real**: inadimplência não é perdida no dia em que alguém
cancela o contrato no sistema, e sim um mês depois do último bloqueio. Os demais
motivos contam na data do cancelamento.

Python puro, sem Django: recebe texto e datas, devolve o veredito. Quem busca
contratos e resolve o nome do motivo é a camada de aplicação.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum

# =============================================================================
# Níveis de controle — a fila de trabalho, na ordem de prioridade
# =============================================================================


class Nivel(StrEnum):
    DEVERIA = "deveria ter retido"
    PODERIA = "poderia ter retido"
    PARCIAL = "parcialmente atacável"
    INEVITAVEL = "inevitável"
    FORA = "fora do churn"


@dataclass(frozen=True)
class NivelInfo:
    nivel: Nivel
    prioridade: str  # rótulo da fila ("1 · Ação imediata")
    slug: str  # chave estável p/ cor e querystring


NIVEIS: tuple[NivelInfo, ...] = (
    NivelInfo(Nivel.DEVERIA, "1 · Ação imediata", "deveria"),
    NivelInfo(Nivel.PODERIA, "2 · Próxima oportunidade", "poderia"),
    NivelInfo(Nivel.PARCIAL, "3 · Influência de processo", "parcial"),
    NivelInfo(Nivel.INEVITAVEL, "4 · Monitorar", "inevitavel"),
    NivelInfo(Nivel.FORA, "5 · Excluir da meta", "fora"),
)

NIVEL_SLUGS: frozenset[str] = frozenset(info.slug for info in NIVEIS)

# Retenção direta = o que a operação podia ter segurado. "Parcialmente
# atacável" fica de fora de propósito: chamar influência parcial de retenção
# garantida infla a meta com o que a empresa não controla.
RETENCAO_DIRETA: frozenset[Nivel] = frozenset({Nivel.DEVERIA, Nivel.PODERIA})


# =============================================================================
# Famílias — o que o motivo cadastrado admite sem ser inconsistente
# =============================================================================


class Familia(StrEnum):
    INADIMPLENCIA = "inadimplencia"
    CONCORRENCIA = "concorrencia"
    MUDANCA = "mudanca"
    TECNICO = "tecnico"
    VOLUNTARIA = "voluntaria"
    DESISTENCIA = "desistencia"
    TITULARIDADE = "titularidade"
    SISTEMICO = "sistemico"
    FALECIMENTO = "falecimento"


# =============================================================================
# Causas raiz — destino, nível e justificativa de cada uma
# =============================================================================


@dataclass(frozen=True)
class CausaInfo:
    destino: str
    nivel: Nivel
    justificativa: str
    familia: Familia


_DEST_OPERADORA = "Troca para outra operadora"
_DEST_MUDANCA = "Mudança de endereço"
_DEST_OPCAO = "Cancelamento por opção"

INADIMPLENCIA = "Inadimplência"
CONC_SEM_MOTIVO = "Concorrência — sem motivo informado"
CONC_COMBO = "Concorrência — combo móvel + fibra"
CONC_PRECO = "Concorrência — preço/proposta"
CONC_INSTALADA = "Concorrência já instalada/contratada"
MUD_SEM_COBERTURA = "Mudança para local sem cobertura"
MUD_NAO_VERIFICADA = "Mudança — cobertura não verificada"
MUD_NAO_INFORMADO = "Mudança — endereço não informado"
MUD_INTERNET_EXISTENTE = "Mudança — internet já existente no local"
MUD_INVIABILIDADE = "Mudança — inviabilidade física de instalação"
MUD_CONCORRENTE = "Mudança/endereço com concorrente"
MUD_DENTRO_COBERTURA = "Mudança — dentro da área de cobertura"
IMOVEL_DESOCUPADO = "Imóvel/ponto desocupado"
VOL_PERDA = "Desconexão voluntária / perda de necessidade"
VOL_SEM_MOTIVO = "Desconexão voluntária — sem motivo informado"
VOL_FINANCEIRA = "Restrição financeira voluntária"
VOL_PRODUTO = "Cancelamento de produto/serviço específico"
DESISTENCIA = "Desistência da assinatura"
TITULARIDADE = "Troca de titularidade"
SISTEMICO = "Ajuste/migração sistêmica"
FALECIMENTO = "Falecimento"
TEC_TERCEIRO = "Problema percebido em serviço de terceiro"
TEC_LENTIDAO = "Problema técnico — lentidão"
TEC_QUEDAS = "Problema técnico — quedas/oscilação"
TEC_EQUIPAMENTO = "Problema técnico — equipamento"
TEC_WIFI = "Problema técnico — Wi-Fi/cobertura interna"
TEC_VISITA = "Problema técnico — visita/atendimento técnico"
TEC_NAO_DETALHADO = "Problema técnico — não detalhado"

# As técnicas não estão aqui: destino e nível delas dependem de haver troca de
# operadora e reincidência — ver `_veredito_tecnico`.
CAUSAS: dict[str, CausaInfo] = {
    INADIMPLENCIA: CausaInfo(
        "Cancelamento após atraso/bloqueio", Nivel.PARCIAL,
        "A régua de cobrança pode reduzir parte das perdas, mas a base não "
        "permite assumir recuperação integral.",
        Familia.INADIMPLENCIA,
    ),
    CONC_SEM_MOTIVO: CausaInfo(
        _DEST_OPERADORA, Nivel.PODERIA,
        "A observação registra apenas a troca, sem explicar por que a "
        "concorrência venceu.",
        Familia.CONCORRENCIA,
    ),
    CONC_COMBO: CausaInfo(
        _DEST_OPERADORA, Nivel.PODERIA,
        "A observação explicita pacote convergente com telefonia/dados móveis.",
        Familia.CONCORRENCIA,
    ),
    CONC_PRECO: CausaInfo(
        _DEST_OPERADORA, Nivel.PODERIA,
        "A troca foi motivada por oferta mais atraente; havia chance de "
        "contraproposta, sem garantia de retenção.",
        Familia.CONCORRENCIA,
    ),
    CONC_INSTALADA: CausaInfo(
        _DEST_OPERADORA, Nivel.PODERIA,
        "Quando o pedido chegou, a concorrência já estava contratada ou "
        "instalada, reduzindo a margem de retenção.",
        Familia.CONCORRENCIA,
    ),
    MUD_SEM_COBERTURA: CausaInfo(
        "Mudança de endereço/cidade sem atendimento", Nivel.INEVITAVEL,
        "A observação confirma mudança para cidade/endereço sem cobertura.",
        Familia.MUDANCA,
    ),
    MUD_NAO_VERIFICADA: CausaInfo(
        _DEST_MUDANCA, Nivel.PODERIA,
        "A observação não permite concluir se havia cobertura; deveria haver "
        "verificação, mas não é correto afirmar que o cliente seria retido.",
        Familia.MUDANCA,
    ),
    MUD_NAO_INFORMADO: CausaInfo(
        _DEST_MUDANCA, Nivel.PODERIA,
        "Sem endereço informado não é possível assumir falta de cobertura; a "
        "verificação deveria ser tentada.",
        Familia.MUDANCA,
    ),
    MUD_INTERNET_EXISTENTE: CausaInfo(
        _DEST_MUDANCA, Nivel.PARCIAL,
        "O novo local já possuía serviço; havia atuação possível, mas retenção "
        "limitada.",
        Familia.MUDANCA,
    ),
    MUD_INVIABILIDADE: CausaInfo(
        _DEST_MUDANCA, Nivel.PARCIAL,
        "A observação registra tentativa de instalação inviabilizada pelas "
        "condições ou autorização do imóvel.",
        Familia.MUDANCA,
    ),
    # Não existia na auditoria original: lá, "temos cobertura" escrito na
    # observação caía em "cobertura não verificada". Mas aqui a verificação foi
    # feita e deu positivo — a transferência era possível e não aconteceu.
    MUD_DENTRO_COBERTURA: CausaInfo(
        _DEST_MUDANCA, Nivel.PODERIA,
        "A observação indica que o novo endereço é atendido; cabia oferecer a "
        "transferência do serviço.",
        Familia.MUDANCA,
    ),
    MUD_CONCORRENTE: CausaInfo(
        _DEST_OPERADORA, Nivel.PODERIA,
        "A troca ocorreu no contexto de mudança; deveria haver checagem de "
        "cobertura e oferta de transferência.",
        Familia.MUDANCA,
    ),
    IMOVEL_DESOCUPADO: CausaInfo(
        "Encerramento por imóvel sem usuário", Nivel.PARCIAL,
        "A necessidade do acesso deixou de existir; caberia tentar "
        "transferência ou indicação, com baixa controlabilidade.",
        Familia.MUDANCA,
    ),
    VOL_PERDA: CausaInfo(
        _DEST_OPCAO, Nivel.PARCIAL,
        "A observação indica perda de uso/interesse; há atuação possível, mas "
        "nem sempre existe necessidade a preservar.",
        Familia.VOLUNTARIA,
    ),
    VOL_SEM_MOTIVO: CausaInfo(
        _DEST_OPCAO, Nivel.PODERIA,
        "O motivo real não foi registrado; exige sondagem obrigatória na "
        "retenção.",
        Familia.VOLUNTARIA,
    ),
    VOL_FINANCEIRA: CausaInfo(
        "Cancelamento para redução de despesas", Nivel.PODERIA,
        "Havia espaço para oferta temporária ou adequação de plano, sem "
        "garantia de recuperação.",
        Familia.VOLUNTARIA,
    ),
    VOL_PRODUTO: CausaInfo(
        "Descontinuação de TV ou internet isolada", Nivel.PARCIAL,
        "A observação mostra retirada de um produto específico, não uma troca "
        "motivada pela concorrência.",
        Familia.VOLUNTARIA,
    ),
    DESISTENCIA: CausaInfo(
        "Desistência antes/ao início do serviço", Nivel.PODERIA,
        "Onboarding e contato rápido poderiam reduzir a desistência, sem "
        "evidência de recuperação certa.",
        Familia.DESISTENCIA,
    ),
    TITULARIDADE: CausaInfo(
        "Contrato transferido para outro titular", Nivel.FORA,
        "Saída administrativa compensada pela continuidade/entrada do contrato "
        "em outro titular.",
        Familia.TITULARIDADE,
    ),
    SISTEMICO: CausaInfo(
        "Baixa administrativa ou saneamento de base", Nivel.FORA,
        "Registro indica ajuste, teste ou contrato já cancelado, e não perda "
        "comercial no período.",
        Familia.SISTEMICO,
    ),
    FALECIMENTO: CausaInfo(
        "Encerramento por falecimento", Nivel.INEVITAVEL,
        "Falecimento é evento inevitável e não atribuível à retenção.",
        Familia.FALECIMENTO,
    ),
}

_DEST_TEC = "Cancelamento por experiência técnica"
_DEST_TEC_CONCORRENTE = "Troca para concorrente após falha técnica"

# Subtipo técnico → causa raiz.
_TEC_CAUSA: dict[str, str] = {
    "Terceiros/IPTV/câmeras": TEC_TERCEIRO,
    "Wi-Fi/cobertura interna": TEC_WIFI,
    "Atraso/ausência de visita": TEC_VISITA,
    "Lentidão": TEC_LENTIDAO,
    "Quedas/oscilação": TEC_QUEDAS,
    "Equipamento/ONU/roteador": TEC_EQUIPAMENTO,
    "Outros/não detalhado": TEC_NAO_DETALHADO,
}

# Toda causa que o classificador pode devolver — o catálogo do filtro da página.
CAUSAS_CONHECIDAS: frozenset[str] = frozenset({*CAUSAS, *_TEC_CAUSA.values()})

NAO_SE_APLICA = "Não se aplica"

# Subtipo de concorrência exibido no card de mudança/concorrência.
_SUBTIPO_CONCORRENCIA: dict[str, str] = {
    CONC_SEM_MOTIVO: "Sem motivo informado",
    CONC_COMBO: "Combo móvel + fibra",
    CONC_PRECO: "Preço/proposta",
    CONC_INSTALADA: "Concorrente já instalado",
    MUD_SEM_COBERTURA: "Local explicitamente não atendido",
    MUD_NAO_VERIFICADA: "Cobertura não verificada",
    MUD_NAO_INFORMADO: "Endereço não informado",
    MUD_INTERNET_EXISTENTE: "Internet já existente",
    MUD_INVIABILIDADE: "Inviabilidade física",
    MUD_CONCORRENTE: "Mudança/endereço",
    MUD_DENTRO_COBERTURA: "Dentro da cobertura",
}


# =============================================================================
# Motivo cadastrado → tipo
# =============================================================================


class MotivoTipo(StrEnum):
    """O que o atendente escolheu no menu, agrupado pelo que importa aqui."""

    INADIMPLENCIA = "inadimplencia"
    OPERADORA = "operadora"
    OPCAO = "opcao"
    ENDERECO = "endereco"
    NAO_CABEADO = "nao_cabeado"
    CIDADE = "cidade"
    TECNICO = "tecnico"
    TITULARIDADE = "titularidade"
    SISTEMICO = "sistemico"
    IMPORTADO = "importado"
    DESISTENCIA = "desistencia"
    FALECIMENTO = "falecimento"
    NENHUM = "nenhum"  # sem motivo, ou motivo que o mapa não conhece


# Ordem importa: "Desistência assinatura- não informado" é desistência, não
# "sem motivo"; "Mudou de operadora" não pode cair em mudança de endereço.
_MOTIVO_TIPO: tuple[tuple[str, MotivoTipo], ...] = (
    ("inadimpl", MotivoTipo.INADIMPLENCIA),
    ("operadora", MotivoTipo.OPERADORA),
    ("provedor", MotivoTipo.OPERADORA),
    ("titularidade", MotivoTipo.TITULARIDADE),
    ("falecimento", MotivoTipo.FALECIMENTO),
    ("desist", MotivoTipo.DESISTENCIA),
    ("por opcao", MotivoTipo.OPCAO),
    ("nao cabeado", MotivoTipo.NAO_CABEADO),
    ("cidade", MotivoTipo.CIDADE),
    ("endereco", MotivoTipo.ENDERECO),
    ("tecnic", MotivoTipo.TECNICO),
    ("sgp", MotivoTipo.IMPORTADO),
    ("acerto sistemico", MotivoTipo.SISTEMICO),
    ("navegacao", MotivoTipo.SISTEMICO),
    ("teste", MotivoTipo.SISTEMICO),
)

# Família que cada tipo de motivo admite. Causa fora dela = inconsistência.
_COMPATIVEL: dict[MotivoTipo, frozenset[Familia]] = {
    MotivoTipo.INADIMPLENCIA: frozenset({Familia.INADIMPLENCIA}),
    MotivoTipo.OPERADORA: frozenset({Familia.CONCORRENCIA}),
    MotivoTipo.OPCAO: frozenset({Familia.VOLUNTARIA}),
    MotivoTipo.ENDERECO: frozenset({Familia.MUDANCA}),
    MotivoTipo.NAO_CABEADO: frozenset({Familia.MUDANCA}),
    MotivoTipo.CIDADE: frozenset({Familia.MUDANCA}),
    MotivoTipo.TECNICO: frozenset({Familia.TECNICO}),
    MotivoTipo.TITULARIDADE: frozenset({Familia.TITULARIDADE}),
    MotivoTipo.SISTEMICO: frozenset({Familia.SISTEMICO}),
    MotivoTipo.IMPORTADO: frozenset({Familia.SISTEMICO}),
    MotivoTipo.DESISTENCIA: frozenset({Familia.DESISTENCIA, Familia.VOLUNTARIA}),
    MotivoTipo.FALECIMENTO: frozenset({Familia.FALECIMENTO}),
}


def normalizar(texto: str) -> str:
    """Minúsculo, sem acento e com espaços colapsados — base de toda regex."""
    decomposto = unicodedata.normalize("NFKD", texto.lower())
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return " ".join(sem_acento.split())


def motivo_tipo(motivo: str) -> MotivoTipo:
    """Tipo do motivo a partir do NOME cadastrado no IXC (não do id)."""
    norm = normalizar(motivo)
    for trecho, tipo in _MOTIVO_TIPO:
        if trecho in norm:
            return tipo
    return MotivoTipo.NENHUM


# =============================================================================
# Detectores — cada um responde uma pergunta sobre a observação normalizada
# =============================================================================


def _re(*alternativas: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{a})" for a in alternativas))


# Palavras que, logo antes do achado, invertem ou suspendem o sentido: "não
# entrava em contato há muito tempo" não é reincidência; "não teve nenhum
# atendimento de problema técnico" não é queixa; "perguntei se recebeu uma
# proposta" não é proposta; "não falou se foi troca de operadora" não é troca.
_NEGACAO = re.compile(r"\b(nao|nunca|sem|nenhum|nenhuma)\b")
_PERGUNTA = re.compile(
    r"\b(se (ele|ela|o cliente|a cliente|foi|era|e|iria|ia)|perguntei|verificar|questionei)\b"
)
_DUVIDA = re.compile(f"{_NEGACAO.pattern}|{_PERGUNTA.pattern}")


def _afirmado(
    padrao: re.Pattern[str], texto: str, guarda: re.Pattern[str], *, palavras: int
) -> bool:
    """Algum achado do padrão que a guarda não anule nas palavras anteriores.

    A janela é curta de propósito: "não está na fidelidade, está se mudando"
    tem um "não" que não tem nada a ver com a mudança.
    """
    for achado in padrao.finditer(texto):
        antes = " ".join(texto[: achado.start()].split()[-palavras:])
        if not guarda.search(antes):
            return True
    return False


# Concorrentes citados nas observações. Power Net e Lideri NÃO entram: são
# bases incorporadas pela própria Velus.
_OPERADORAS = r"vivo|claro|tim|oi|starlink|poxnet|desktop|sumicity|digital play|sorocabana|algar|unifique"

_CONCORRENTE = _re(
    r"\b(trocou|trocado|troca|trocar|mudou|mudar) (de|para a|para|pra) (operadora|provedor)",
    r"\b(outra|nova) (operadora|empresa|internet)\b",
    r"\b(outro|novo) provedor\b",
    r"\bnova internet\b",
    r"\bja (contrat|instal|fech|coloc|assin)",
    r"\b(contratou|contratar|contratando|fechou|fechar|assinou|instalou|instalaram|colocaram) "
    r"(com )?(a |uma |o |um )?(outra|outro|nova|novo)\b",
    rf"\b(a|da|na|pela|pra|para|com) ({_OPERADORAS})\b",
    r"\bstarlink\b|\bpoxnet\b",
    r"\binstalaram\b|\bpreferiu contratar\b",
    r"\bja (esta|foi) instalad",
)

_COMBO = _re(
    r"dados mov",
    r"\bcombo\b",
    r"\bpacote\b",
    r"\b(internet|linha|plano|telefonia) movel\b",
)
# "Celular" sozinho é o telefone do cliente ("não atende o celular"); só vira
# pacote convergente quando a observação fala de troca.
_CELULAR = _re(r"\bcelular\b", r"\bchip\b")

# Oferta explícita da concorrência. Preço solto ("taxa de R$ 90") só conta
# junto de troca de operadora — ver `_proposta`.
_OFERTA = _re(
    r"\breceb\w* (uma )?(outra )?(propost|ofert)",
    r"\bpropost\w* (melhor|mais)",
    r"\b(melhor|outra) propost",
    r"\bofert",
    r"\boferec(eram|eu)\b",
    r"\bpromoc",
)
_PRECO = _re(r"mais barat", r"\bpreco\b", r"r\$", r"\bcustos?\b")

_JA_INSTALADA = _re(
    r"\bja (contrat|instal|fech|coloc|assin)",
    r"\b(instalou|instalaram|colocaram)\b",
    r"\bja (esta|foi) instalad",
    r"\bja (tem|possui) outra (internet|operadora)\b",
    r"\bpagando duas\b|\bduas internets?\b",
    rf"\bcontratou (a |uma |o )?(outra|outro|nova|novo|{_OPERADORAS})\b",
)

_MUDANCA = _re(
    r"\bse mud(ou|ar|ando|a|aram|ei)\b",
    r"\bmud(ou|ar|ando|aram|ara) (de |para |pra )?(de )?"
    r"(casa|endereco|cidad|estado|pais|bairro|residencia|condominio|local)",
    # "mudou para o Rio Grande do Sul" é mudança; "mudou para a Vivo", não.
    r"\bmud(ou|ar|ando|aram|ara) (para|pra) (?!(a |uma |o |um )?(outra |nova |outro |novo )?"
    rf"(operadora|provedor|plano|empresa|internet|{_OPERADORAS})\b)",
    r"\b(de|em|uma|um|motivo) mudanca\b",
    r"\bmudanca (de|para|pra) (?!(o |a )?(banco|titular|plano|operadora|velocidade|vencimento|senha))",
    r"\b(indo|foi|vai|ira|iria|irao|vao|for|precisou|precisa|pretende) (se )?(mudar|morar)\b",
    r"\bmorar (em|no|na|num|numa|fora|com|junto)\b",
    r"\bsaindo (da casa|do local|do imovel|da residencia|do endereco)\b",
    r"\b(trocou|trocar|troca) de casa\b",
    r"\b(novo|nova) (endereco|casa|residencia)\b|\bendereco novo\b",
    r"\bvoltou para (o interior|a cidade|sua cidade)\b",
    r"\b(vendeu|alugou) (a |sua )?(casa|chacara|imovel|apartamento)\b",
    r"\balug(ou|ando|ado) (a |sua |o )?(casa|imovel|apartamento|para)\b",
    r"\bmorando (longe|em outr|fora)\b",
    # Com destino: "o inquilino foi embora" não é o cliente se mudando.
    r"\b(foi|indo|vai) embora (pra|para|de)\b",
)

# Lugares citados nas observações que ficam fora da área atendida
# (Sorocaba/Votorantim). Itu e Campinas NÃO entram: há clientes lá.
_FORA_DA_AREA = (
    r"nordeste|bahia|santa catarina|mato grosso|sao paulo|rio de janeiro|minas gerais|"
    r"parana|goias|pernambuco|ceara|espirito santo|brasilia|rio grande|aracoiaba|"
    r"itarare|sao roque|cananeias?|piedade|barueri|itapeva|maua|praia|interior|exterior"
)

_SEM_COBERTURA = _re(
    r"\b(nao|sem) (temos |tem |ha |possuimos )?cobertura\b",
    r"\bnao atende(mos)?\b",
    r"\bnao temos o (novo )?local\b",
    r"\bfora da (nossa )?(area|regiao)\b",
    r"\bnao cabeado\b|\bsem cabeamento\b",
    r"\bmud\w* de (cidad|estado|pais)",
    r"\b(outr[oa]) (cidad|estado|pais)",
    r"\bembora do pais\b|\bfora do pais\b|\bmorar fora\b",
    rf"\b({_FORA_DA_AREA})\b",
)

_INTERNET_EXISTENTE = _re(
    r"\bja (tem|possui|possuia|tinha|existe) (uma |a |nossa |outra )?internet\b",
    r"\bja tem outro contrato\b",
    r"\binternet (ja )?inclusa\b",
    r"\binternet foi instalada no local\b",
    r"\bficar (apenas |so |somente )?com o outro (contrato|ponto)\b",
    rf"\bja (tinha|tem|possui) (a )?({_OPERADORAS})\b",
    r"\bja (e|eh) (nosso|nossa) client",
)

# Mudança para endereço que a empresa atende: a transferência era possível.
_DENTRO_COBERTURA = _re(
    r"\b(onde|la|que|pois) (temos|tem|ha) cobertura\b",
    r"\bdentro da (nossa )?(area|regiao)\b",
    r"\b(onde|que) atendemos\b",
)

_INVIABILIDADE = _re(
    r"\bnao (foi|e) possivel (realizar |fazer )?(a )?instala",
    r"\bnao permite\b",
    r"\b(so|nao) (e |foi )?permitid",
    r"\bnao autoriz",
    r"\binviab",
)

_NAO_INFORMOU_ENDERECO = _re(
    r"\bnao quis (me |nem )?(passar|informar|dizer|falar)",
    r"\bnao (informou|passou) (o )?(novo )?endereco\b",
)

# "Financeiro" solto não serve: "sócia e financeiro da empresa" é cargo. Só a
# situação financeira do cliente conta.
_FINANCEIRO = _re(
    r"\bdesempreg",
    r"\b(razoes|razao|motivos?|problemas?|questoes|questao|reorganizacao|situacao|"
    r"dificuldades?|condicoes|crise|vida) financeir",
    r"\breduzir (os |seus )?(custos|gastos|despesas)\b",
    r"\breducao (de|dos) (custos|gastos|despesas)\b",
    r"\bcorte (de|dos) (custos|gastos|despesas)\b",
    r"\bcortar (os )?(custos|gastos|despesas)\b",
    r"\bdificuldade financeira\b",
    r"\bnao (ira |vai |vao )?(conseguir|consegue|tem condic\w*|tera condic\w*) (mais )?(de )?pagar\b",
    r"\bsem condic\w* de pagar\b",
    r"\bnao (vao|vai|ira|irao) (mais )?arcar\b",
)

_PRODUTO = _re(
    r"\b(apenas|somente|so) (a |o )?(tv|televisao)\b",
    r"\bcancelar apenas (a |o )?(internet|tv|televisao)\b",
    r"\bficar com a (tv|televisao)\b",
    r"\bso possuia (a )?(tv|televisao)\b",
    r"\b(manter|ficar com) (a)?penas (a |o )?(internet|tv)\b",
    r"\bcancel\w* (apenas )?(a |o |do )?(tv|televisao|plano de tv)\b",
    r"\btv aberta\b",
)

# Arrependimento no prazo do consumidor: desistência, qualquer que seja o motivo
# escolhido no menu.
_DESISTENCIA_TXT = _re(r"\b(7|sete) dias\b", r"\bprazo (do consumidor|de arrependimento)\b", r"\barrepend")

_IMOVEL = _re(
    r"\bfechar o ponto\b", r"\bponto da loja\b", r"\bno local nao ficou ninguem\b",
    r"\bentreg\w* o ponto\b", r"\blocal (esta|ficou|ficara) (vazio|parado|fechado)\b",
)

# Recusa explícita de dizer o motivo — distinta de "sem motivo" escrito à mão,
# que na desistência é só o atendente sem ter o que registrar.
_RECUSOU_MOTIVO = _re(
    r"\bnao (quis|deseja|quer) (me |nos )?(informar|dizer|falar|passar)\b",
    r"\bnao (informou|disse|deu) (o )?motivo\b",
    r"\bnao (me |nos )?respondeu\b",
)
_SEM_MOTIVO = _re(_RECUSOU_MOTIVO.pattern, r"\bsem motivo\b")

_TITULARIDADE = _re(
    r"\btitul(ari|id)", r"\bdoou a conta\b", r"\bdoador\b",
    # Recontratado no nome de outra pessoa da casa: o cliente ficou.
    r"\b(nova contratacao|novo contrato) (da nossa|no nome|em nome)",
)
_FALECIMENTO = _re(r"\bfalec", r"\bmorreu\b", r"\bobito\b")
_SISTEMICO_TXT = _re(
    r"\bcontrato errado\b", r"\bduplic", r"\btestes?\b", r"\bsgp\b", r"\bpermuta\b",
    r"\bveio (ativ|cancel)", r"\bnao (teve|houve) (navegacao|consumo|utilizacao)\b",
    r"\bsem navegacao\b", r"\bnunca (se conectou|teve utilizacao)\b", r"\bcolaborador\b",
    # Contratos que chegaram ativos das bases incorporadas mas já estavam
    # cancelados lá, e recadastros.
    r"\bcriad\w* (outro|um novo|novo) (contrato|cadastro)\b",
    r"\b(nao|nunca) (navegou|teve navegacao|teve consumo)\b", r"\bsem consumo\b",
    r"\b(veio|subiu|chegou) (para nos )?ativ",
    r"\bcancel\w* (com|na) (a )?(power ?net|lideri|sgp)\b",
    r"\bja tinha cancelado\b", r"\bera para estar cancelado\b",
)
_INADIMPLENCIA_TXT = _re(
    r"\binadimpl", r"\bfalta de pagamento\b", r"\bnao pagou\b", r"\bter sido bloquead",
)

# Técnico: subtipos em ordem de prioridade. "Equipamento" é o último porque
# roteador e ONU aparecem em quase toda observação de cancelamento (devolução
# do aparelho), e aí não dizem nada sobre a causa.
_TEC_SUBTIPOS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Terceiros/IPTV/câmeras", _re(r"\biptv\b", r"\bapp terceiro\b", r"\bterceiro\b", r"\bcameras?\b")),
    ("Wi-Fi/cobertura interna", _re(
        r"\bcasa toda\b", r"\bcomodos?\b", r"\b(distante|longe) do roteador\b",
        r"\bwi-?fi (fraco|ruim|nao)\b", r"\bsinal (do )?wi-?fi\b", r"\balcance\b",
        r"\b(2\.4|5 ?g(hz)?)\b",
    )),
    ("Atraso/ausência de visita", _re(
        r"\bnao (enviamos|enviaram|mandamos|mandaram|foi enviado)( um| o)? tecnico\b",
        r"\btecnico (nao|nunca) (veio|foi|apareceu)\b",
        r"\bagenda dos tecnicos\b",
        r"\b(atraso|demora) (na|da|para a|pra) (visita|retirada|instalacao)\b",
        r"\bmanutencao agendada\b",
        r"\b(esperar|aguardar) (a |uma )?(visita|manutencao|solucao)\b",
        r"\bnao teve (retorno|atendimento)\b|\bsem retorno\b",
    )),
    ("Lentidão", _re(r"\blent[oa]s?\b", r"\blentidao\b", r"\bdevagar\b")),
    ("Quedas/oscilação", _re(
        r"\bquedas?\b", r"\boscil", r"\bcaindo\b", r"\bcai\b", r"\bdesconecta", r"\brompimento\b",
        r"\b(ficou|fica|esta|estava) sem (internet|conexao|sinal)\b", r"\blos\b",
        r"\binstabilidade\b", r"\binstavel\b",
    )),
    ("Equipamento/ONU/roteador", _re(r"\broteador\b", r"\bonu\b", r"\bmodem\b", r"\bequipamento\b")),
)

# Queixa técnica genérica — usada para reclassificar motivos que não são
# técnicos ("Desconexão por opção" + "internet trava muito").
_TEC_GENERICO = _re(
    r"\bproblemas? (tecnic|de conexao|com a (nossa )?(internet|conexao|rede)|"
    r"ao usar (a |nossa )?(internet|conexao)|na (internet|conexao))",
    r"\b(internet|conexao|servico) (vinha |vem |estava |esta |ficava )?(dando|com) "
    r"(muito |muitos )?problema",
    r"\bnao funciona",
    r"\bnunca funciona",
    r"\b(internet|conexao|servico) (esta |estava |ta )?(ruim|pessim[ao]|instavel)\b",
    r"\bpessimo servico\b",
    r"\bnunca (solucion|resolv)",
    r"\bsolucao tecnica\b",
    r"\bproblemas? (com|no|na|desde) (o |a )?(roteador|onu|modem|equipamento|wi-?fi|sinal|dia)",
    r"\binsatisf\w* com (a |o |as )?(nossa |nosso )?(internet|conexao|servico)\b",
    r"\bnao solucion",
    r"\btrava",
    r"\bqualidade (do|da) (sinal|internet|conexao|servico)\b",
    r"\bsinal (ruim|fraco|pessimo)\b",
    r"\bdescontente com (a |nossa |o |nosso )?(internet|conexao|servico)\b",
    r"\bsempre (teve|tinha|tive) problemas?\b",
    r"\breclam\w* (muito |bastante )?(da|de|com) (a |o )?(nossa |nosso )?(internet|conexao|servico|sinal)\b",
    r"\bqualidade (caiu|piorou|ruim|pessima|baixa)\b",
    # As marcas da própria base: Power Net e Lideri foram incorporadas.
    r"\bproblemas? com a (power ?net|lideri|liderei|velus)\b",
)

_REINCIDENCIA = _re(
    r"\bsempre (teve|tive|tinha|dava|deu|tem|esteve)\b.{0,20}problema",
    r"\b(varias|diversas|muitas) vezes\b",
    r"\breclam\w* (bastante|muito|varias|diversas|sempre)\b",
    r"\b(a|ha|faz) (muito|bastante) tempo\b",
    r"\bdesde a epoca\b",
    r"\breincid",
    r"\b(varios|muitos|diversos) (chamados|atendimentos|protocolos|visitas)\b",
)

# Sinais de perda de necessidade — separam "por opção, e disse por quê" de
# "por opção, sem dizer nada" quando o motivo não é o de desconexão.
_PERDA = _re(
    r"\bnao (usa|utiliza|precisa|quer mais|deseja (mais )?(manter|seguir))\b",
    r"\bnao (ira|vai|irei) (mais )?(precisar|usar|utilizar)\b",
    r"\bserventia\b|\bsem uso\b",
    r"\bpessoa(l|is)\b",
    r"\bfech(ou|ar|ando) (a |o |seu |sua )?(empresa|comercio|loja|escritorio|estabelecimento)\b",
    r"\bencerr",
    r"\bnao tem mais (interesse|necessidade)\b|\bsem necessidade\b",
    r"\bnao usara\b",
    r"\bficar (apenas |so |somente )?com (1|um|o outro) (contrato|ponto)\b",
)


# A visita é o único subtipo cujos padrões já carregam a negação ("não
# enviamos técnico", "não quis aguardar a manutenção") — negar de novo apagaria
# justamente o achado.
_SUBTIPO_JA_NEGATIVO = "Atraso/ausência de visita"


def _tecnico_subtipo(texto: str) -> str | None:
    """Subtipo técnico citado (e não negado), ou None."""
    for subtipo, padrao in _TEC_SUBTIPOS:
        if subtipo == _SUBTIPO_JA_NEGATIVO:
            if padrao.search(texto):
                return subtipo
        elif _afirmado(padrao, texto, _NEGACAO, palavras=3):
            return subtipo
    return None


def _tem_queixa_tecnica(texto: str) -> bool:
    """Queixa técnica forte o bastante para vencer um motivo não técnico.

    Equipamento sozinho não conta: devolução de roteador é rotina em toda
    observação de cancelamento e não diz nada sobre a causa.
    """
    subtipo = _tecnico_subtipo(texto)
    if subtipo is not None and subtipo != "Equipamento/ONU/roteador":
        return True
    return _afirmado(_TEC_GENERICO, texto, _DUVIDA, palavras=3)


def _reincidente(texto: str) -> bool:
    return _afirmado(_REINCIDENCIA, texto, _NEGACAO, palavras=4)


def _concorrente(texto: str) -> bool:
    return _afirmado(_CONCORRENTE, texto, _PERGUNTA, palavras=3)


def _mudanca(texto: str) -> bool:
    return _afirmado(_MUDANCA, texto, _NEGACAO, palavras=2)


def _combo(texto: str, *, troca: bool = False) -> bool:
    """Pacote convergente. `troca=True` quando o motivo já diz que houve troca."""
    if _COMBO.search(texto):
        return True
    return bool(_CELULAR.search(texto)) and (troca or _concorrente(texto))


def _proposta(texto: str) -> bool:
    if _afirmado(_OFERTA, texto, _PERGUNTA, palavras=5):
        return True
    return bool(_PRECO.search(texto)) and _concorrente(texto)


# =============================================================================
# Veredito
# =============================================================================


@dataclass(frozen=True)
class Veredito:
    causa_raiz: str
    destino_final: str
    nivel: Nivel
    justificativa: str
    familia: Familia
    inconsistente: bool
    subtipo_tecnico: str = NAO_SE_APLICA
    subtipo_concorrencia: str = NAO_SE_APLICA


def _veredito(causa: str) -> Veredito:
    info = CAUSAS[causa]
    return Veredito(
        causa_raiz=causa,
        destino_final=info.destino,
        nivel=info.nivel,
        justificativa=info.justificativa,
        familia=info.familia,
        inconsistente=False,
        subtipo_concorrencia=_SUBTIPO_CONCORRENCIA.get(causa, NAO_SE_APLICA),
    )


def _veredito_tecnico(texto: str) -> Veredito:
    """Falha técnica: reincidência sobe para "deveria"; troca muda o destino."""
    subtipo = _tecnico_subtipo(texto) or "Outros/não detalhado"
    reincidente = _reincidente(texto)
    concorrente = _concorrente(texto)

    if reincidente:
        # "Outros + reincidência" é só "Reincidência": o subtipo não diz nada.
        rotulo = "Reincidência" if subtipo == "Outros/não detalhado" else f"{subtipo} + reincidência"
    else:
        rotulo = subtipo

    justificativa = "A observação aponta falha técnica como causa anterior ao cancelamento"
    if concorrente:
        justificativa += " e posterior troca de operadora"
    return Veredito(
        causa_raiz=_TEC_CAUSA[subtipo],
        destino_final=_DEST_TEC_CONCORRENTE if concorrente else _DEST_TEC,
        nivel=Nivel.DEVERIA if reincidente else Nivel.PODERIA,
        justificativa=justificativa + ".",
        familia=Familia.TECNICO,
        inconsistente=False,
        subtipo_tecnico=rotulo,
        subtipo_concorrencia="Problema técnico anterior" if concorrente else NAO_SE_APLICA,
    )


def _causa_mudanca(texto: str, *, padrao: str) -> str:
    """Mudança: o que a observação diz sobre o destino do cliente."""
    if _DENTRO_COBERTURA.search(texto):
        return MUD_DENTRO_COBERTURA
    if _SEM_COBERTURA.search(texto):
        return MUD_SEM_COBERTURA
    if _INTERNET_EXISTENTE.search(texto):
        return MUD_INTERNET_EXISTENTE
    if _concorrente(texto):
        return MUD_CONCORRENTE
    if _INVIABILIDADE.search(texto):
        return MUD_INVIABILIDADE
    if _NAO_INFORMOU_ENDERECO.search(texto):
        return MUD_NAO_INFORMADO
    return padrao


def _causa_concorrencia(texto: str, *, troca: bool = False) -> str:
    if _combo(texto, troca=troca):
        return CONC_COMBO
    if _proposta(texto):
        return CONC_PRECO
    if _JA_INSTALADA.search(texto):
        return CONC_INSTALADA
    return CONC_SEM_MOTIVO


def _administrativo(texto: str) -> str | None:
    """Saídas que não são perda comercial (ou nem escolha do cliente), escritas
    na observação de um motivo genérico: titularidade, falecimento, contrato de
    teste/duplicado, bloqueio por falta de pagamento."""
    if _TITULARIDADE.search(texto):
        return TITULARIDADE
    if _FALECIMENTO.search(texto):
        return FALECIMENTO
    if _SISTEMICO_TXT.search(texto):
        return SISTEMICO
    if _INADIMPLENCIA_TXT.search(texto):
        return INADIMPLENCIA
    return None


def _classificar_opcao(texto: str) -> Veredito | str:
    """"Desconexão por opção" — o motivo mais genérico, e o que mais esconde."""
    administrativo = _administrativo(texto)
    if administrativo is not None:
        return administrativo
    if _tem_queixa_tecnica(texto):
        return _veredito_tecnico(texto)
    if _PRODUTO.search(texto):
        return VOL_PRODUTO
    if _FINANCEIRO.search(texto):
        return VOL_FINANCEIRA
    if _combo(texto):
        return CONC_COMBO
    if _proposta(texto):
        return CONC_PRECO
    if _mudanca(texto):
        return _causa_mudanca(texto, padrao=MUD_NAO_VERIFICADA)
    if _DESISTENCIA_TXT.search(texto):
        return DESISTENCIA
    if _IMOVEL.search(texto):
        return IMOVEL_DESOCUPADO
    if _SEM_MOTIVO.search(texto):
        return VOL_SEM_MOTIVO
    return VOL_PERDA


def _classificar_operadora(texto: str) -> Veredito | str:
    if _tem_queixa_tecnica(texto):
        # O motivo já diz que houve troca; a observação só não repetiu.
        return replace(
            _veredito_tecnico(texto),
            destino_final=_DEST_TEC_CONCORRENTE,
            justificativa=(
                "A observação aponta falha técnica como causa anterior ao "
                "cancelamento e posterior troca de operadora."
            ),
            subtipo_concorrencia="Problema técnico anterior",
        )
    if _mudanca(texto):
        return MUD_CONCORRENTE
    return _causa_concorrencia(texto, troca=True)


def _causa_de_churn(texto: str) -> Veredito | str | None:
    """Causa de perda comercial que a observação afirma, ou None.

    É o que decide quando o motivo cadastrado não serve: vazio, desconhecido,
    ou administrativo mas com uma história de cliente de verdade na observação
    ("não houve navegação" + "cancelou por corte de custos").
    """
    if _INADIMPLENCIA_TXT.search(texto):
        return INADIMPLENCIA
    if _tem_queixa_tecnica(texto):
        return _veredito_tecnico(texto)
    if _FINANCEIRO.search(texto):
        return VOL_FINANCEIRA
    if _combo(texto):
        return CONC_COMBO
    if _proposta(texto):
        return CONC_PRECO
    if _mudanca(texto):
        return _causa_mudanca(texto, padrao=MUD_NAO_VERIFICADA)
    if _concorrente(texto):
        return _causa_concorrencia(texto)
    if _IMOVEL.search(texto):
        return IMOVEL_DESOCUPADO
    if _PERDA.search(texto):
        return VOL_PERDA
    return None


def _classificar_sem_motivo(texto: str) -> Veredito | str:
    """Motivo vazio ou desconhecido: a observação é tudo o que existe."""
    administrativo = _administrativo(texto)
    if administrativo is not None:
        return administrativo
    if _PRODUTO.search(texto):
        return VOL_PRODUTO
    if _DESISTENCIA_TXT.search(texto):
        return DESISTENCIA
    return _causa_de_churn(texto) or VOL_SEM_MOTIVO


def _classificar_sistemico(texto: str) -> Veredito | str:
    """Acerto, teste, "não houve navegação": fora do churn — salvo quando a
    observação conta a história de um cliente que foi embora."""
    if _TITULARIDADE.search(texto):
        return TITULARIDADE
    if _SISTEMICO_TXT.search(texto):
        return SISTEMICO
    return _causa_de_churn(texto) or SISTEMICO


def _classificar_desistencia(texto: str) -> Veredito | str:
    """Desistência: no IXC virou motivo genérico em 2024–25, então a observação
    decide quando ela diz outra coisa (mudança, concorrência, falha técnica)."""
    administrativo = _administrativo(texto)
    if administrativo is not None:
        return administrativo
    if _RECUSOU_MOTIVO.search(texto):
        return VOL_SEM_MOTIVO
    return _causa_de_churn(texto) or DESISTENCIA


def classificar(motivo: str, observacao: str) -> Veredito:
    """Causa raiz, destino, nível de controle e inconsistência de um cancelamento.

    `motivo` é o NOME do motivo cadastrado no IXC; `observacao`, o texto livre
    do atendente (`obs_cancelamento`).
    """
    tipo = motivo_tipo(motivo)
    texto = normalizar(observacao or "")

    resultado: Veredito | str
    if tipo is MotivoTipo.INADIMPLENCIA:
        resultado = INADIMPLENCIA
    elif tipo is MotivoTipo.TITULARIDADE:
        resultado = TITULARIDADE
    elif tipo is MotivoTipo.FALECIMENTO:
        resultado = FALECIMENTO
    elif tipo is MotivoTipo.IMPORTADO:
        # Contrato que já chegou cancelado na migração do SGP: é histórico de
        # outra base, nunca perda desta — qualquer que seja a observação.
        resultado = SISTEMICO
    elif tipo is MotivoTipo.SISTEMICO:
        resultado = _classificar_sistemico(texto)
    elif tipo is MotivoTipo.DESISTENCIA:
        resultado = _classificar_desistencia(texto)
    elif tipo is MotivoTipo.TECNICO:
        resultado = _veredito_tecnico(texto)
    elif tipo is MotivoTipo.OPERADORA:
        resultado = _classificar_operadora(texto)
    elif tipo is MotivoTipo.OPCAO:
        resultado = _classificar_opcao(texto)
    elif tipo is MotivoTipo.CIDADE:
        resultado = _causa_mudanca(texto, padrao=MUD_SEM_COBERTURA)
    elif tipo in (MotivoTipo.ENDERECO, MotivoTipo.NAO_CABEADO):
        resultado = _causa_mudanca(texto, padrao=MUD_NAO_VERIFICADA)
    else:
        resultado = _classificar_sem_motivo(texto)

    veredito = _veredito(resultado) if isinstance(resultado, str) else resultado
    compativel = _COMPATIVEL.get(tipo)
    # Sem motivo cadastrado não há o que contradizer.
    inconsistente = compativel is not None and veredito.familia not in compativel
    return replace(veredito, inconsistente=inconsistente)


# =============================================================================
# Competência real
# =============================================================================

REGRA_CANCELAMENTO = "Data de cancelamento"
REGRA_BLOQUEIO_AUTO = "Bloqueio automático mais recente + 30 dias"
REGRA_BLOQUEIO_MANUAL = "Bloqueio manual mais recente + 30 dias"

DIAS_APOS_BLOQUEIO = 30


@dataclass(frozen=True)
class Competencia:
    data: date  # o dia em que o churn passa a contar
    base: date  # a data que originou o cálculo
    regra: str


def competencia(
    causa_raiz: str,
    cancelado_em: date,
    *,
    bloqueio_auto: date | None,
    bloqueio_manual: date | None,
) -> Competencia:
    """Mês em que o cancelamento realmente pesa.

    Inadimplência: o cliente foi perdido quando parou de pagar, não quando
    alguém fez a baixa no sistema — conta o bloqueio mais recente (automático
    ou manual) mais 30 dias. Sem bloqueio registrado, cai na data de
    cancelamento. Qualquer outra causa conta na data de cancelamento.

    Recebe a causa raiz (do `classificar`), não o motivo: inadimplência escrita
    só na observação também desloca a competência.
    """
    if causa_raiz == INADIMPLENCIA:
        bloqueios = [
            (dia, regra)
            for dia, regra in (
                (bloqueio_auto, REGRA_BLOQUEIO_AUTO),
                (bloqueio_manual, REGRA_BLOQUEIO_MANUAL),
            )
            if dia is not None
        ]
        if bloqueios:
            base, regra = max(bloqueios, key=lambda b: b[0])
            return Competencia(base + timedelta(days=DIAS_APOS_BLOQUEIO), base, regra)
    return Competencia(cancelado_em, cancelado_em, REGRA_CANCELAMENTO)
