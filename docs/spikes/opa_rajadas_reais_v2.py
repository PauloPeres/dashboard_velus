# -*- coding: utf-8 -*-
"""Rajadas reais v2 — de onde vem cada mensagem que mandamos, e quanto dela é "a mais".

=============================================================================
COMO RODAR (dentro do pod de produção; só faz GET na API pública do Opa)
=============================================================================
Rode do seu computador, na pasta onde está este arquivo. O nome do pod sai do
próprio deploy/web (o hostname de um pod é o nome dele), então o `cp` e o
`exec` caem no MESMO pod — no v1 o `exec deploy/web` podia cair em outra réplica.

  POD=$(kubectl exec -n dashboard-velus deploy/web -c web -- hostname)
  kubectl cp opa_rajadas_reais_v2.py dashboard-velus/$POD:/tmp/opa_rajadas_reais_v2.py -c web

  # (opcional) textos completos dos nós, direto do painel. Sem ele o script usa
  # a lista embutida abaixo (textos dos fluxos em 21/09/2026).
  kubectl cp fluxos.json dashboard-velus/$POD:/tmp/fluxos.json -c web

  # (opcional, recomendado primeiro) autoteste sem tocar na API:
  kubectl exec -n dashboard-velus $POD -c web -- env OPA_V2_AUTOTESTE=1 \
    python manage.py shell -v0 -c "import runpy; runpy.run_path('/tmp/opa_rajadas_reais_v2.py', run_name='__main__')"

  # análise (20 páginas de 1000 = ~20 mil mensagens, a ponta da coleção):
  kubectl exec -n dashboard-velus $POD -c web -- env OPA_V2_PAGINAS=20 \
    python manage.py shell -v0 -c "import runpy; runpy.run_path('/tmp/opa_rajadas_reais_v2.py', run_name='__main__')" \
    > opa_v2_saida.txt

  # só o JSON:
  sed -n '/===JSON_INICIO===/,/===JSON_FIM===/p' opa_v2_saida.txt | sed '1d;$d' > opa_v2.json

  # limpeza:
  kubectl exec -n dashboard-velus $POD -c web -- rm -f /tmp/opa_rajadas_reais_v2.py /tmp/fluxos.json

O progresso sai no stderr (aparece no terminal); o resumo legível e o JSON saem
no stdout (vão para o arquivo). O estilo do v1 também funciona:
  ... python manage.py shell -v0 -c "exec(open('/tmp/opa_rajadas_reais_v2.py').read())"

Custo na API (só GET, 4 req/s pelo cliente do projeto): ~22 chamadas de
bissecção + 20 páginas + 1 chamada por conversa que começa com mensagem nossa
(teto 800) → de 1 a ~4 minutos. `fluxos.json` traz a estrutura inteira dos
fluxos (inclusive URL de webhook dos nós httpRequest): se copiar, apague depois.

=============================================================================
SAÍDA — chaves do JSON (entre ===JSON_INICIO=== e ===JSON_FIM===)
=============================================================================
  janela, conversas, totais            tamanho da amostra, rajadas, excesso (mesma
                                       definição do v1) e `comparacao_v1`
  esquema                              campos que a API devolve (nomes/formas; valor
                                       só de campos de tipo/status)
  campo_atendente_humano               calibração: qual campo (se algum) separa
                                       atendente de bot, com P(marca|humano/bot)
  campo_destinatario                   idem para o destinatário
  por_categoria                        enviadas/excesso (síncrono/assíncrono)/fora da
                                       janela por origem, com a fonte da decisão
  por_no_e_texto                       o mesmo por nó de fluxo ou texto
  nos_alcancaveis_sem_envio_na_janela  nós vivos que não mandaram nada
  por_causa                            excesso por "origem anterior → origem atual"
                                       (fluxo→fluxo separado em mesmo/entre fluxos)
  tempo_dentro_da_rajada               faixas <5s/5–60s/1–10min/>10min, síncrono x
                                       follow-up, follow-up por categoria
  pares_top, trincas_top               sequências mais comuns, com intervalo mediano
  duplicidades                         mesmo texto 2x seguidas; mesmo ASSUNTO na mesma
                                       rajada/conversa, e entre quais nós/fluxos
  caminhos                             saudação cliente/não-cliente e ramo do
                                       diagnóstico (sem conexão, online, atraso,
                                       bloqueado, massiva, reduzida…)
  desfecho_das_perguntas               o que vem depois de cada pergunta/menu
                                       (opção escolhida pelo nó seguinte, ou follow-up)
  inatividade                          "ainda está aí"/"sessão expirou": depois de
                                       quê, quanto tempo (≈ timeout configurado), e se
                                       veio humano/cliente depois
  proativas                            conversas que começam com mensagem nossa
  gigantes                             conversas > LIMIAR_GIGANTE e mensagens sem
                                       id_rota: destinatários distintos, repetições
  fora_da_janela_24h                   envioForaJanela24h=True (o que a Meta cobra)
  nao_classificadas_top, notas

Parâmetros (variáveis de ambiente, passadas com `env` como acima):
  OPA_V2_PAGINAS=20            páginas lidas da ponta da coleção
  OPA_V2_PAGE=1000             tamanho da página (a API aceita 1000; medido)
  OPA_V2_GIGANTE=100           conversa com mais que isso vai para a análise à parte
  OPA_V2_SINCRONO_S=60         intervalo que separa rajada síncrona de follow-up
  OPA_V2_VERIFICAR_INICIO=1    confere pela API (1 chamada por conversa) se a
                               conversa que começa com mensagem nossa começou
                               mesmo assim ou só foi cortada pela janela
  OPA_V2_MAX_VERIFICACOES=800  teto dessas chamadas
  OPA_V2_K_MIN=3               texto nosso só é impresso se aparece em >= K conversas
  OPA_V2_K_MIN_HUMANO=5        idem, para texto de atendente humano
  OPA_V2_TOP=30                tamanho dos rankings
  OPA_V2_CAMPO_HUMANO=...      força o campo que marca mensagem de atendente
  OPA_V2_CAMPO_DESTINATARIO=.. força o campo que identifica o destinatário
  OPA_V2_FLUXOS=/caminho.json  fluxos.json em outro lugar

=============================================================================
O QUE MUDOU DO v1
=============================================================================
1. Máscara ANTES de agrupar: nome depois de "Olá", "me chamo", ator de evento
   ("Fulana alterou o departamento"), datas, horas, números longos (protocolo,
   telefone, CPF), R$, código PIX (000201…), URL, e-mail, arquivo → placeholders.
   Chave = texto mascarado inteiro normalizado (não mais os 55 primeiros
   caracteres crus, que faziam "Olá {nome}, seu protocolo…" virar uma chave por
   cliente e sumir do ranking).
2. Cada mensagem ENVIADA ganha uma origem: fluxo (casada com o texto de um nó),
   integração (PIX, escolha de título, "opção inválida!" da fatura), inatividade,
   pesquisa, humano, template/disparo, sistema (evento de troca de departamento),
   aviso fora de fluxo, ou não classificada. Cada uma diz a FONTE da decisão:
   `campo` (um campo da API), `texto_no` (texto igual ao de um nó), `texto_padrao`
   (texto conhecido fora de fluxo), `heuristica_*` (contexto da conversa).
3. Intervalo de tempo dentro da rajada: separa o que o fluxo manda de uma vez
   (síncrono) do que chega depois de silêncio (follow-up de timeout, pesquisa,
   humano).
4. Contagens por nó/fluxo e por causa, caminhos depois da saudação, inatividade,
   conversas gigantes (destinatários distintos), conversas que começam com
   mensagem nossa, e duplicidade de conteúdo ENTRE fluxos (o mesmo "assunto" —
   protocolo, App, transferência, avaliação — dito duas vezes).

=============================================================================
LGPD — o que este script NUNCA imprime
=============================================================================
- Texto de mensagem RECEBIDA (do cliente): não é nem mascarado, é descartado.
- Nome, telefone, CPF, protocolo, código PIX, e-mail, id de conversa/cliente.
- Texto nosso que não passou pela máscara com segurança: só sai no ranking se
  o texto mascarado aparece em >= K conversas distintas (K_MIN; K_MIN_HUMANO
  para texto de atendente). Texto que aparece em poucas conversas é o que tem
  mais chance de carregar dado pessoal — esse sai como "(texto oculto)".
- Valores de campos da API só são impressos para campos de "tipo"/"status"
  (lista branca); o resto sai como contagem de valores distintos.

Tudo que o script conta é MEDIDO na amostra. Onde a origem é inferida por
heurística, o campo `fonte` diz qual. Onde algo não pôde ser medido, o valor é
null e há um `motivo`.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo

    TZ_SP = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    TZ_SP = None


# =============================================================================
# 0. Parâmetros
# =============================================================================
def _env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, "") or padrao)
    except ValueError:
        return padrao


PAGINAS = _env_int("OPA_V2_PAGINAS", 20)
PAGE = _env_int("OPA_V2_PAGE", 1000)
LIMIAR_GIGANTE = _env_int("OPA_V2_GIGANTE", 100)
LIMIAR_SINCRONO_S = _env_int("OPA_V2_SINCRONO_S", 60)
VERIFICAR_INICIO = os.environ.get("OPA_V2_VERIFICAR_INICIO", "1") == "1"
MAX_VERIFICACOES = _env_int("OPA_V2_MAX_VERIFICACOES", 800)
K_MIN = _env_int("OPA_V2_K_MIN", 3)
K_MIN_HUMANO = _env_int("OPA_V2_K_MIN_HUMANO", 5)
TOP_N = _env_int("OPA_V2_TOP", 30)
CAMPO_HUMANO_FORCADO = os.environ.get("OPA_V2_CAMPO_HUMANO") or None
CAMPO_DESTINATARIO_FORCADO = os.environ.get("OPA_V2_CAMPO_DESTINATARIO") or None
CAMINHOS_FLUXOS = [
    os.environ.get("OPA_V2_FLUXOS") or "",
    "/tmp/fluxos.json",
    "/tmp/opa/fluxos.json",
    "fluxos.json",
]
TETO_BISSECCAO = 4_000_000
VERSAO = "v2 (2026-09-21)"

DT_MIN = datetime(1970, 1, 1, tzinfo=timezone.utc)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# =============================================================================
# 1. Normalização e máscara
# =============================================================================
def sem_acentos(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def texto_de(valor):
    """(texto, forma, chaves_do_dict, marca_template) de `mensagem`.

    Mensagem interativa (lista/botões) chega como dict: `{'titulo': ..., 'opcoes': [...]}`
    — foi isso que apareceu como "{'titulo': 'por favor, escolha…" no v1. O texto
    que casa com o nó é o `titulo`.
    """
    if valor is None:
        return "", "vazio", None, False
    if isinstance(valor, dict):
        chaves = sorted(str(k) for k in valor.keys())
        tpl = any(re.search(r"template|hsm|components", k, re.I) for k in chaves)
        for k in ("titulo", "title", "texto", "text", "body", "corpo", "pergunta",
                  "mensagem", "caption", "legenda", "descricao"):
            v = valor.get(k)
            if isinstance(v, str) and v.strip():
                return v, "dict", chaves, tpl
            if isinstance(v, dict):
                t2, _, _, tpl2 = texto_de(v)
                if t2:
                    return t2, "dict", chaves, tpl or tpl2
        partes = [v for v in valor.values() if isinstance(v, str)]
        return " ".join(partes), "dict", chaves, tpl
    if isinstance(valor, list):
        partes = [texto_de(v)[0] for v in valor]
        return " ".join(p for p in partes if p), "lista", None, False
    s = str(valor)
    st = s.strip()
    if st.startswith("{") and ("titulo" in st or "title" in st):
        for parser in (json.loads, ast.literal_eval):
            try:
                d = parser(st)
            except Exception:
                continue
            if isinstance(d, dict):
                t, _, ch, tpl = texto_de(d)
                return t, "dict_em_texto", ch, tpl
    return s, "str", None, False


_NOME_PALAVRA = r"[A-Za-zÀ-ÖØ-öø-ÿ'´`-]+"
_STOP_NOME = set("""
eu nosso nossa nossos nossas tudo bem me sou seja somos estamos temos como que a o os as um uma
voce voces vc cliente clientes prezado prezada gostaria informamos obrigado obrigada sim nao aqui
e bom boa dia tarde noite pessoal equipe velus time novamente de do da dos das para por favor entao
ok certo podemos pode poderia verifiquei verificamos desculpe muito recebemos seu sua informo segue
seguem so tenho estou vamos vou ja agora hoje amanha sr sra srta senhor senhora dona dr dra
tudo td blz beleza gente amigo amiga querido querida caro cara estimado estimada
""".split())

RX_BR = re.compile(r"<br\s*/?>", re.I)
RX_PIX = re.compile(r"000201.{10,700}?6304[0-9A-Fa-f]{4}", re.S)
RX_PIX2 = re.compile(r"000201\S{10,}")
RX_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
RX_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
RX_ARQ = re.compile(r"\b[\w\-.]{1,80}\.(?:pdf|jpe?g|png|gif|webp|ogg|opus|mp3|mp4|m4a|wav|docx?|xlsx?|csv|txt)\b", re.I)
RX_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")
RX_CNPJ = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
RX_CEP = re.compile(r"\b\d{5}-\d{3}\b")
RX_VALOR = re.compile(r"R\$\s*\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|R\$\s*\d+(?:[.,]\d{1,2})?", re.I)
RX_DATA = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")
RX_HORA = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b|\b\d{1,2}h\d{2}\b", re.I)
RX_NUMGRUPOS = re.compile(r"\b\d{3,}(?:[ .-]\d{3,}){2,}\b")
RX_FONE = re.compile(r"(?:\+?55\s?)?(?:\(\d{2}\)|\b\d{2})\s?9?\d{4}[-\s]?\d{4}\b")
RX_NUMLONGO = re.compile(r"\b[A-Za-z]{0,6}\d{5,}[A-Za-z0-9]*\b")
_NOME_CAP = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'´`-]*"          # palavra que começa maiúscula
_NOME_SEQ = _NOME_CAP + r"(?:\s+(?:(?i:d[aeo]s?)\s+)?" + _NOME_CAP + r"){0,4}"  # "João da Silva", "JOÃO DA SILVA"
RX_ASSINATURA = re.compile(r"^\s*\*([A-Za-zÀ-ÖØ-öø-ÿ'´ ]{2,40})\*\s*(?::|\n)")
# Case-insensitive só no gatilho ((?i:…)); o nome precisa começar com maiúscula,
# senão "me chamo Rodrigo e vou seguir" engoliria "e vou seguir".
RX_TRATAMENTO = re.compile(r"\b((?i:sr|sra|srta|senhor|senhora|dona|dr|dra))(\.?\s+)(" + _NOME_SEQ + r")")
RX_APRESENTA = re.compile(
    r"\b((?i:me chamo|meu nome [eé]|sou (?:o|a)))(\s+)(" + _NOME_PALAVRA + r"(?:\s+" + _NOME_CAP + r"){0,3})"
)
RX_ATOR = re.compile(
    r"^\s*([A-Za-zÀ-ÖØ-öø-ÿ'´.\- ]{2,60}?)\s+(alterou|transferiu|encerrou|finalizou|assumiu|adicionou|removeu|iniciou|reabriu)\b",
    re.I,
)
# Destino de evento de sistema: departamento fica, nome de pessoa sai.
RX_EVENTO_DESTINO = re.compile(
    r"\b((?:alterou o departamento do atendimento|transferiu o atendimento|atendimento transferido|foi transferido) para)"
    r"(\s+(?:o |a )?)([^\n.,;!?]{1,60})", re.I)
_DEPTOS = set("""
financeiro fin suporte sup comercial com triagem agendamento agendamentos renegociacao renegociacoes vendas ouvidoria
tecnico cobranca retencao instalacao manutencao noc departamento setor fila atendimento humano sac
""".split())
RX_SAUDACAO = re.compile(
    r"\b(ol[áa]|oi|bom dia|boa tarde|boa noite|prezad[oa](?:\(a\))?|estimad[oa])([,!]?\s+)([^\n,!.?:;<{]{1,60})(?=[,!.?:;\n]|$)",
    re.I,
)
RX_CLIENTE_NOME = re.compile(r"\b((?i:cliente|titular))(\s+)(" + _NOME_CAP + r"(?:\s+(?:(?i:d[aeo]s?)\s+)?" + _NOME_CAP + r"){1,5})")


def _troca_destino(m):
    resto = m.group(3).strip()
    prim = sem_acentos(resto.lower()).split()[:1]
    if prim and prim[0] in _DEPTOS:
        return m.group(0)
    return m.group(1) + m.group(2) + "<NOME>"


def _troca_saudacao(m):
    cand = m.group(3).strip()
    palavras = cand.split()
    if not (1 <= len(palavras) <= 6):
        return m.group(0)
    if not all(re.fullmatch(_NOME_PALAVRA, p) for p in palavras):
        return m.group(0)
    if sem_acentos(palavras[0].lower()) in _STOP_NOME:
        return m.group(0)
    return m.group(1) + m.group(2) + "<NOME>"


def mascarar(texto):
    """Troca dado pessoal/variável por placeholder. Aplicada igual no texto do nó e
    no texto enviado, então o casamento não depende do que foi mascarado."""
    t = unicodedata.normalize("NFKC", str(texto or ""))
    t = RX_BR.sub("\n", t)
    t = RX_PIX.sub("<PIX>", t)
    t = RX_PIX2.sub("<PIX>", t)
    t = RX_URL.sub("<URL>", t)
    t = RX_EMAIL.sub("<EMAIL>", t)
    t = RX_ARQ.sub("<ARQUIVO>", t)
    t = RX_CPF.sub("<DOC>", t)
    t = RX_CNPJ.sub("<DOC>", t)
    t = RX_CEP.sub("<CEP>", t)
    t = RX_VALOR.sub("<VALOR>", t)
    t = RX_DATA.sub("<DATA>", t)
    t = RX_HORA.sub("<HORA>", t)
    t = RX_NUMGRUPOS.sub("<NUM>", t)
    t = RX_FONE.sub("<FONE>", t)
    t = RX_NUMLONGO.sub("<NUM>", t)
    t = RX_ASSINATURA.sub("<ATENDENTE>:\n", t)
    t = RX_TRATAMENTO.sub(lambda m: m.group(1) + m.group(2) + "<NOME>", t)
    t = RX_APRESENTA.sub(lambda m: m.group(1) + m.group(2) + "<NOME>", t)
    t = RX_ATOR.sub(lambda m: "<NOME> " + m.group(2), t)
    t = RX_EVENTO_DESTINO.sub(_troca_destino, t)
    t = RX_SAUDACAO.sub(_troca_saudacao, t)
    t = RX_CLIENTE_NOME.sub(lambda m: m.group(1) + m.group(2) + "<NOME>", t)
    return " ".join(t.split())


def chave(masc):
    """Chave de agrupamento: texto mascarado inteiro, sem acento/pontuação/emoji."""
    s = sem_acentos(str(masc or "").lower())
    s = s.replace("{ph}", " _ph_ ")
    s = re.sub(r"<([a-z]+)>", r" _\1_ ", s)
    s = re.sub(r"[^a-z0-9_]+", " ", s)
    return " ".join(s.split())


def busca(masc):
    """Texto para as regex de catálogo: sem acento, minúsculo, pontuação mantida."""
    return " ".join(sem_acentos(str(masc or "").lower()).split())


def rotulo(masc, n=140):
    s = str(masc or "").strip()
    return (s[: n - 1] + "…") if len(s) > n else (s or "(sem texto)")


_RX_LGPD = [
    re.compile(r"\d{5,}"),
    re.compile(r"000201"),
    re.compile(r"@"),
    re.compile(r"\d{3}\.\d{3}\.\d{3}"),
    re.compile(r"\(\d{2}\)\s?\d{4}"),
]


def lgpd_ok(s):
    return not any(rx.search(s or "") for rx in _RX_LGPD)


def _h(v):
    return hashlib.sha1(str(v).encode("utf-8", "ignore")).hexdigest()[:16]


# =============================================================================
# 2. Catálogos de texto fora dos fluxos (ordem importa)
# =============================================================================
INATIVIDADE = [
    ("transferencia_por_inatividade", re.compile(r"\bainda esta ai\b")),
    ("sessao_expirou", re.compile(r"sua sessao expirou")),
    ("pesquisa_encerrada_inatividade", re.compile(r"pesquisa.{0,60}encerrad|encerrad.{0,60}inatividade")),
]
PESQUISA = [
    ("pesquisa_1a5", re.compile(r"clique no botao .{0,3}ver menu.{0,3} e escolha uma opcao de 1 a 5")),
    ("pesquisa_0a5", re.compile(r"avalie nosso atendimento,? de 0 a 5")),
    ("pesquisa_agradecimento", re.compile(r"obrigad[oa] (pela|por) (sua )?(avaliacao|nota|participar da pesquisa|responder)")),
]
INTEGRACAO_FORTE = [
    ("pix_cabecalho", re.compile(r"^segue (o )?codigo pix")),
    ("pix_codigo", re.compile(r"<pix>|br\.gov\.bcb\.pix")),
    ("escolha_titulo", re.compile(r"escolha uma opcao:? ?1 ?- ?titulo|titulo com vencimento")),
]
SISTEMA = [
    ("troca_departamento", re.compile(r"alterou o departamento")),
    ("transferencia_manual", re.compile(r"transferiu o atendimento|atendimento transferido por")),
    ("encerramento_manual", re.compile(r"(encerrou|finalizou) o atendimento")),
    ("atendente_assumiu", re.compile(r"assumiu o atendimento|entrou no atendimento")),
]
HUMANO_PADROES = [
    ("apresentacao_atendente", re.compile(r"\bme chamo\b|\bmeu nome e\b|vou seguir com o seu atendimento|vou dar continuidade")),
    ("assinatura_atendente", re.compile(r"^<atendente>:")),
    ("despedida_atendente", re.compile(r"agradecemos pelo contato|foi um prazer atender")),
    ("comercial_sem_cobertura", re.compile(r"infelizmente (ainda )?nao temos cobertura")),
    ("comercial_planos", re.compile(r"temos cobertura no seu endereco|seguem os (nossos )?planos|\bmbps\b.{0,40}<valor>")),
]
AVISO_FORA_FLUXO = [
    ("aviso_atendimento_temporariamente_encerrado", re.compile(r"temporariamente encerrad")),
    ("aviso_rompimento_resolvido", re.compile(r"rompimento da fibra (ja )?foi resolvido|conexao (ja )?foi (normalizada|restabelecida)")),
    ("aviso_instabilidade", re.compile(r"comunicado velus|instabilidade (generalizada|em nossa rede)|manutencao (emergencial|programada)")),
]
COBRANCA = [
    ("cobranca_mensalidades", re.compile(r"consta em nosso sistema que ha mensalidade|mensalidades? em aberto|faturas? (em aberto|vencid)")),
]
INTEGRACAO_HEUR = [
    ("nota_fiscal_heur", re.compile(r"\bnota fiscal\b|\bnfs?-?e\b")),
    ("desbloqueio_resultado_heur", re.compile(r"(desbloqueio|liberacao) (em|de) confianca (foi |realizad|efetuad|concluid)|contrato (foi )?(desbloqueado|liberado)")),
    ("boleto_heur", re.compile(r"linha digitavel|codigo de barras")),
]

# "Assunto" de cada mensagem — para achar o mesmo conteúdo dito duas vezes
# (inclusive por fluxos diferentes). Regex sobre `busca()`.
TAGS = [
    ("protocolo", re.compile(r"\bprotocolo\b")),
    ("anuncio_app", re.compile(r"\bapp velus\b|aplicativo velus|play store|app store|baixe agora")),
    ("pix_recorrente", re.compile(r"pix recorrente")),
    ("horario_atendimento", re.compile(r"horario de atendimento")),
    # "vou te transferir…" e "iniciaremos o atendimento…" dão a MESMA notícia ao
    # cliente (vai falar com gente) — por isso um assunto só.
    ("transferencia_para_atendente", re.compile(
        r"transferi(do|da|r|ndo|mos)\b|te passar para (um|o)\b|passando para um humano|direcionado ao nosso|"
        r"te enviando para um|repassando a solicitacao|sera transferido|iniciaremos o atendimento|"
        r"em breve retornaremos|logo um de nossos atendentes|atendentes humanos ira")),
    ("pede_avaliacao", re.compile(r"avalie nosso atendimento|para avaliar nosso atendimento|de 0 a 5")),
    ("despedida", re.compile(r"obrigado por escolher|agradecemos pelo contato|foi um prazer atender|obrigado por contar conosco|atendimento encerrado")),
    ("pergunta_mais_ajuda", re.compile(r"posso te ajudar com mais alguma coisa|tem mais alguma coisa que posso te ajudar")),
    ("erro_nao_entendi", re.compile(r"opcao invalida|nao entendi|nao consegui entender|algo saiu errado|opcao valida")),
    ("saudacao", re.compile(r"^(ola|oi|bom dia|boa tarde|boa noite)\b|seja bem vindo")),
    ("menu_opcoes", re.compile(r"escolha uma das opcoes|digite uma das das opcoes|escolha uma opcao")),
    ("problema_rede", re.compile(r"problema de conexao|problema generalizado|instabilidade|rompimento")),
    ("encerra_sessao", re.compile(r"sessao expirou|temporariamente encerrad")),
]


def tags_de(b):
    return {nome for nome, rx in TAGS if rx.search(b)}


# =============================================================================
# 3. Catálogo de nós: fluxos.json (opcional) ou lista embutida
# =============================================================================
ENTRADAS_FLUXO = [
    "00 - Checar de onde veio",
    "Pesquisa de Satisfação",
    "Interativo - Cobranca 19 dias automatico",
    "TAG CUSTOMER",
]


def textos_do_no(n):
    """(papel, texto) que um nó do grafo pode mandar."""
    t = n.get("type")
    out = []
    if t == "msg":
        for m in n.get("mensagens") or []:
            v = m.get("value") if isinstance(m, dict) else None
            if isinstance(v, str) and v.strip():
                out.append(("msg", v))
    elif t == "perg":
        if isinstance(n.get("pergunta"), str) and n["pergunta"].strip():
            out.append(("pergunta", n["pergunta"]))
        for e in n.get("msg_erro") or []:
            if isinstance(e, str) and e.strip():
                out.append(("erro", e))
    elif t == "pesquisa-satisfacao":
        if isinstance(n.get("pergunta"), str) and n["pergunta"].strip():
            out.append(("pesquisa", n["pergunta"]))
    elif t == "opt_in_opt_out":
        for k in ("mensagemOptIn", "mensagemOptOut"):
            if isinstance(n.get(k), str) and n[k].strip():
                out.append(("msg", n[k]))
    lim = n.get("msg_qtd_erro_excedida")
    if isinstance(lim, str) and lim.strip():
        out.append(("limite", lim))
    return out


def _pos_init(e):
    for j, x in enumerate(e):
        if x.get("type") == "init":
            return j
    return 0


def nos_de_fluxos_json(caminho):
    with open(caminho, encoding="utf-8") as fh:
        d = json.load(fh)
    rows = d["rows"] if isinstance(d, dict) else d
    est, nomes = {}, {}
    for f in rows:
        e = f.get("estrutura")
        e = json.loads(e) if isinstance(e, str) else (e or [])
        est[f["_id"]] = e
        nomes[f["_id"]] = f.get("nome", "?")
    por_nome = {v: k for k, v in nomes.items()}
    alc = set()
    pilha = [(por_nome[n], _pos_init(est[por_nome[n]])) for n in ENTRADAS_FLUXO if n in por_nome]
    while pilha:
        fid, i = pilha.pop()
        if fid not in est or not (0 <= i < len(est[fid])) or (fid, i) in alc:
            continue
        alc.add((fid, i))
        n = est[fid][i]
        lim = n.get("executarFlowLimiteErros")
        if lim in est:
            pilha.append((lim, _pos_init(est[lim])))
        t = n.get("type")
        if t == "eflow":
            d2 = n.get("eflow_id")
            if d2 in est:
                pilha.append((d2, _pos_init(est[d2])))
            continue
        for k in (("next1", "next") if t == "cond" else ("next",)):
            v = n.get(k)
            if v in (None, ""):
                continue
            try:
                pilha.append((fid, int(v)))
            except (TypeError, ValueError):
                continue
    nos = []
    for fid, e in est.items():
        for i, n in enumerate(e):
            for papel, txt in textos_do_no(n):
                nos.append({"fluxo": nomes[fid], "idx": i, "papel": papel,
                            "texto": txt, "alcancavel": (fid, i) in alc})
    return nos


# Gerado de fluxos.json (painel do Opa, 21/09/2026). (fluxo, posição do nó, papel,
# início do texto com espaços colapsados, alcançável a partir da entrada). Cortados em
# 300 caracteres: o casamento usa só os ~18 primeiros tokens.
# @@NOS_EMBUTIDOS_INICIO@@
NOS_EMBUTIDOS = [
    ('00 - Checar de onde veio', 8, 'msg', '⚠️ Aviso Velus Internet Identificamos uma instabilidade generalizada na sua região. Isso pode ocorrer por fatores como queda de energia, rompimento de fibra ou vandalismo. 👷 Nossa equipe já está atuando com prioridade máxima. 🕒 Previsão de normalização: 18:30 – 10 de Outubro. Pedimos desculpas pelo ', False),
    ('00 - Checar de onde veio', 9, 'msg', '⚠️ COMUNICADO VELUS INTERNET Identificamos uma instabilidade em nossa rede que está afetando o acesso à internet de alguns clientes. 🛠️ Nossa equipe técnica já identificou a situação e está atuando para normalizar o serviço o mais rápido possível. 📌 Não é necessário abrir chamado ou permanecer aguar', False),
    ('00 - Verifica se é cliente', 2, 'msg', 'Olá {{nome_cliente_fornecedor}}, seu protocolo para esse atendimento é: {{protocolo}} 📑 TEMOS UMA NOVIDADE para VOCÊ! Conheça o App Velus 📱 Tudo em um só lugar: suas informações, suporte e PIX Recorrente, deixando seus pagamentos automáticos e sem preocupações. 📲 Baixe agora: • App Store: https://ap', True),
    ('00 - Verifica se é cliente', 4, 'msg', 'Seja bem vindo a Velus, seu protocolo para esse atendimento é: {{protocolo}}', True),
    ('01 - Diagnóstico de Contrato', 1, 'limite', 'Desculpe não entendi, vou enviar para nosso menu principal', True),
    ('01 - Diagnóstico de Contrato', 13, 'limite', 'Desculpe não entendi, vou te passar para um atendente.', False),
    ('01 - Diagnóstico de Contrato', 15, 'msg', 'Verifiquei aqui, que parece que você esta com algum problema de conexão, vou te transferir para o suporte.', True),
    ('01 - Diagnóstico de Contrato', 17, 'limite', 'Desculpe não entendi, vou te passar para um atendente.', False),
    ('01 - Diagnóstico de Contrato', 22, 'limite', 'Não consegui encontrar sua fatura, vou te passar para um atendente.', True),
    ('01 - Diagnóstico de Contrato', 23, 'limite', 'Não consegui encontrar sua fatura, vou te passar para um atendente.', True),
    ('02 - Principal Entrada', 1, 'erro', 'Opção inválida! Escolha uma das opções.', True),
    ('02 - Principal Entrada', 1, 'limite', 'Desculpe não consegui entender o que você gostaria, estou te passando para um humano, para te ajudar.', True),
    ('02 - Principal Entrada', 1, 'pergunta', 'Por favor, escolha uma das opções abaixo.', True),
    ('02 - Principal Entrada', 6, 'msg', 'Seu atendimento foi transferido para o departamento comercial, iniciaremos o atendimento o mais breve possível.', True),
    ('02 - Principal Entrada', 9, 'msg', 'Seu atendimento foi transferido para o departamento suporte técnico, iniciaremos o atendimento o mais breve possível.', True),
    ('02 - Principal Entrada', 13, 'msg', 'Seu atendimento foi transferido para o departamento de Agendamento, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 22:00 horas e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.', True),
    ('Direto para Financeiro', 2, 'msg', 'Seu atendimento foi direcionado ao nosso financeiro. Nosso horário de atendimento é de segunda a sexta, das 08h00 às 12h00 e das 13h30 às 18h00, e aos sábados das 08h00 às 12h00. Em breve retornaremos seu contato. 📱 Para sua comodidade, você também pode utilizar o aplicativo Velus para acessar fatur', True),
    ('Direto para Suporte', 2, 'msg', 'Muito obrigado por entrar em contato 😊, iniciaremos o atendimento o mais breve possível.', True),
    ('Direto para Suporte', 4, 'msg', 'Estamos realizando uma manutenção emergencial na rede neste momento. Durante esse período, sua conexão pode apresentar instabilidades ou interrupções intermitentes entre 2h e 6h. Nossa equipe já está atuando para normalizar o serviço o mais rápido possível. 🚧 Agradecemos pela compreensão!', False),
    ('Direto para Triagem - Atendimento Humano', 2, 'msg', 'Seu atendimento foi transferido para o um atendente humano, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 12:00 e das 13:30 às 18:00 horas e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.', True),
    ('Direto para o Comercial', 1, 'msg', 'Seu atendimento foi transferido para o departamento de Comercial, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 22:00 horas e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.', True),
    ('Direto para o Comercial', 1, 'msg', 'Caso queira contratar nossos planos entre em https://assine.velusinternet.com.br ou aguarde para ser atendido', True),
    ('Encerramento', 1, 'msg', 'Obrigado por escolher a {{nome_empresa}}, estamos sempre trabalhando para entregar a melhor internet para nossos clientes. Para abrir um novo atendimento digite algo.', True),
    ('Encerramento', 2, 'pesquisa', 'Avalie nosso atendimento, de 0 a 5?', True),
    ('Entrada', 1, 'msg', 'Olá eu sou o Víctor seu atendente virtual, seja bem vindo a central de atendimento da #EMPRESA_NOME_FILIAL#!', False),
    ('Facebook Entrada', 1, 'msg', 'Olá, tudo bem? Para agilizar seu atendimento, por gentileza nos fornecer as informações abaixo: - Telefone com DDD - Endereço', False),
    ('Facebook Entrada', 1, 'msg', 'Caso queira, também pode entrar em contato conosco via WhatsApp https://wa.me/551531999869?text=Ol%C3%A1,%20quero%20contratar', False),
    ('Facebook Menu', 1, 'erro', 'Ops, não entendi o que você digitou. Digite apenas os números 1, 2 ou 3, de acordo com sua escolha.', False),
    ('Facebook Menu', 1, 'pergunta', 'Em que podemos te ajudar? Digite o número que corresponde a sua escolha', False),
    ('Facebook Menu', 5, 'msg', 'Entendi. Um de nossos atendentes humanos irá prosseguir com sua solicitação. .', False),
    ('Facebook Menu', 6, 'msg', 'Entendi. Vamos te ajudar o mais rápido possível, estou repassando a solicitação para um atendente humano! Aguarde 15 segundos.', False),
    ('Facebook Menu', 7, 'msg', 'Certo, logo um de nossos atendentes humanos irá te auxiliar no pagamento.', False),
    ('Facebook Retorno', 1, 'erro', 'Ops, digite apenas 1 ou 2, conforme sua necessidade.', False),
    ('Facebook Retorno', 1, 'pergunta', 'O que deseja fazer agora?', False),
    ('Facebook Retorno', 6, 'msg', 'Seu protocolo é: OPA24352355. Obrigado por contar conosco, espero que tenhamos lhe ajudado a resolver todos seus problemas! Fique a vontade para nos deixar seu feedback em nossas redes sociais.', False),
    ('Facebook Retorno', 6, 'msg', 'ATENDIMENTO ENCERRADO.', False),
    ('Falar com atendentes', 1, 'erro', 'Opção inválida', False),
    ('Falar com atendentes', 1, 'pergunta', 'Selecione um departamento', False),
    ('Financeiro', 1, 'erro', 'Opção inválida! Escolha uma das opções.', True),
    ('Financeiro', 1, 'limite', 'Opção invalida, estamos te enviando para um de nossos atendentes.', True),
    ('Financeiro', 1, 'pergunta', 'Digite uma das das opções', True),
    ('Financeiro', 3, 'limite', 'Desculpe, não consegui encontrar seu cadastro, vou te transferir para um atendente.', False),
    ('Financeiro', 5, 'limite', 'Desculpe, não consegui encontrar seu cadastro, vou te transferir para um atendente.', True),
    ('Financeiro', 12, 'msg', 'Seu atendimento foi transferido para o departamento financeiro, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 22:00 e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.', True),
    ('Financeiro', 17, 'msg', 'Seu atendimento foi transferido para o departamento de renegociação, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 22:00 e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.', True),
    ('Financeiro', 19, 'limite', 'Desculpe, não consegui encontrar seu cadastro, vou te transferir para um atendente.', True),
    ('Fluxo Indique um Amigo', 2, 'msg', '*Obrigado por Participar do Nosso Programa de Indicação!* Para facilitar o processo, por favor, digite o *nome* e o *WhatsApp* do seu amigo para que possamos entrar em contato com ele.', True),
    ('Fluxo Indique um Amigo', 4, 'msg', 'Seu atendimento foi transferido para o departamento Comercial. Nosso horário de atendimento é de segunda a sexta-feira, das 08:00 às 22:00, e aos sábados, das 08:00 às 12:00. Iniciaremos o atendimento o mais breve possível.', True),
    ('Interativo - Cobranca 19 dias automatico', 2, 'msg', 'Olá, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 22:00 horas e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.', True),
    ('Intermediário', 1, 'erro', 'Opção inválida! Escolha uma das opções.', True),
    ('Intermediário', 1, 'pergunta', 'Posso te ajudar com mais alguma coisa?', True),
    ('Início', 1, 'msg', 'Olá eu sou o {{nome_atendente}}, atendente virtual da {{nome_empresa}}, fico feliz em atende-lo.', False),
    ('Início', 2, 'erro', 'Ops! algo saiu errado.', False),
    ('Início', 2, 'erro', 'Tente novamente', False),
    ('Início', 2, 'limite', 'Desculpe não entendi, vou te redirecionar para um atendente.', False),
    ('Início', 2, 'pergunta', 'Por favor, digite o numero da opção selecionada.', False),
    ('Início', 7, 'msg', 'Você será transferido ao suporte.', False),
    ('Início', 8, 'msg', 'Você será transferido aos nossos especialistas!', False),
    ('Manutenção SEM BOT', 1, 'msg', 'Gostaríamos de informar que estamos realizando uma manutenção programada em nossos sistemas no momento. Durante esse período, alguns serviços podem ficar temporariamente indisponíveis. Estamos trabalhando diligentemente para minimizar qualquer impacto e restabelecer plenamente os serviços o mais ráp', False),
    ('Manuteção BOT SUPORTE', 2, 'msg', 'Gostaríamos de informar que estamos realizando uma manutenção programada em nossos sistemas no momento. Durante esse período, alguns serviços podem ficar temporariamente indisponíveis. Estamos trabalhando diligentemente para minimizar qualquer impacto e restabelecer plenamente os serviços o mais ráp', False),
    ('Massiva', 1, 'erro', 'Desculpe não entendi, escolha uma das opções.', True),
    ('Massiva', 1, 'pergunta', '💬 Percebi que você está com um problema generalizado na sua região. Tem mais alguma coisa que posso te ajudar?', True),
    ('Nota Fiscal', 1, 'erro', 'Informe uma opção valida!', False),
    ('Nota Fiscal', 1, 'pergunta', 'Você deseja receber suas notas fiscais?', False),
    ('OptIn/OptOut', 1, 'msg', 'Você aceita receber nossas mensagens? Digite 1 para sim, e 2 para não. Você poderá cancelar o envio das mensagens a qualquer momento, apenas enviando no chat: /bloquear.', False),
    ('OptIn/OptOut', 1, 'msg', 'Olá {{nome_usuario}}, tem certeza que deseja parar de receber as novidades e avisos ? Se mudar de ideia, poderá voltar quando quiser. Você interrompeu o envio de informações eventuais.', False),
    ('Pergunta, Desbloqueio em Confiança', 1, 'erro', 'Desculpe não entendi.', True),
    ('Pergunta, Desbloqueio em Confiança', 1, 'limite', 'Desculpe não entendi, vou te passar para o menu principal.', True),
    ('Pergunta, Desbloqueio em Confiança', 1, 'pergunta', 'Seu contrato de Internet esta reduzido, você gostaria de realizar a liberação em confiança?', True),
    ('Pergunta, Desbloqueio em Confiança', 2, 'limite', 'Desculpe não conseguimos realizar a liberação automatica, vou te transferir para um atendente.', True),
    ('Pergunta, Liberação de Redução', 1, 'erro', 'Desculpe não entendi sua resposta.', True),
    ('Pergunta, Liberação de Redução', 1, 'limite', 'Desculpe não entendi, vou te passar para o menu principal.', True),
    ('Pergunta, Liberação de Redução', 1, 'pergunta', 'Seu contrato de Internet esta com velocidade reduzida, você gostaria de realizar o liberação em confiança?', True),
    ('Pergunta, Liberação de Redução', 5, 'limite', 'Desculpe não conseguimos realizar a liberação automatica, vou te transferir para um atendente.', True),
    ('Pesquisa de Satisfação', 1, 'pesquisa', 'Por favor, clique no botão "Ver Menu" e escolha uma opção de 1 a 5 para avaliar nosso atendimento, sendo 1 o menor valor 😞 e 5 o maior 😊. Caso queira encerrar a pesquisa, por favor digite /encerrar. Obrigado!', True),
    ('Teste', 2, 'pergunta', '{mensagem que veio do n8n}', False),
]
# @@NOS_EMBUTIDOS_FIM@@


def carregar_nos():
    for c in CAMINHOS_FLUXOS:
        if c and os.path.isfile(c):
            try:
                nos = nos_de_fluxos_json(c)
                if nos:
                    return nos, f"fluxos.json ({c})"
            except Exception as exc:  # arquivo velho/corrompido → cai na lista
                log(f"[aviso] não consegui ler {c}: {exc!r}; usando lista embutida")
    nos = [{"fluxo": f, "idx": i, "papel": p, "texto": t, "alcancavel": a}
           for (f, i, p, t, a) in NOS_EMBUTIDOS]
    return nos, "lista embutida (fluxos de 21/09/2026)"


N_TOKENS_CASAMENTO = 18
MIN_TOKENS_PREFIXO = 10


def _regex_no(ch, prefixo_livre=False):
    """Regex sobre a chave da mensagem (+ espaço final). Casa quando:
    - os ~18 primeiros tokens do nó aparecem no início da mensagem (placeholder
      {{…}} do nó consome 0–8 tokens: nome, protocolo, nada); ou
    - a mensagem ACABA antes, mas já casou >= 10 tokens literais do nó (texto do
      nó editado depois / mensagem cortada); ou
    - o nó é curto (<= 18 tokens) e a mensagem é exatamente ele."""
    toks = ch.split()
    corte = toks[:N_TOKENS_CASAMENTO]
    partes = [r"(?:\S+ ){0,8}?" if t == "_ph_" else re.escape(t) + " " for t in corte]
    # Pergunta curta pode chegar com as opções coladas no texto: aí vale prefixo.
    completo = len(toks) <= N_TOKENS_CASAMENTO and not (
        prefixo_livre and len([t for t in toks if t != "_ph_"]) >= 4)
    j, lit = len(partes), 0
    for i, t in enumerate(corte):
        if t != "_ph_":
            lit += 1
        if lit >= MIN_TOKENS_PREFIXO:
            j = i + 1
            break
    cauda = "$" if completo else ""
    for p in reversed(partes[j:]):
        cauda = "(?:" + p + cauda + "|$)"
    return re.compile("^" + "".join(partes[:j]) + cauda)


def _placeholders(txt):
    return re.sub(r"\{\{[^}]*\}\}|#[A-Z_]{3,}#", "{PH}", txt)


class Catalogo:
    def __init__(self, nos, origem):
        self.origem = origem
        grupos = {}
        for n in nos:
            masc = mascarar(_placeholders(n["texto"]))
            ch = chave(masc)
            if not [t for t in ch.split() if t != "_ph_"]:
                continue
            g = grupos.setdefault(ch, {"chave": ch, "membros": [], "masc": masc})
            g["membros"].append(n)
        self.grupos = []
        for ch, g in grupos.items():
            g["rx"] = _regex_no(ch, prefixo_livre=any(m["papel"] in ("pergunta", "pesquisa") for m in g["membros"]))
            g["espec"] = len([t for t in ch.split()[:N_TOKENS_CASAMENTO] if t != "_ph_"])
            g["alcancavel"] = any(m["alcancavel"] for m in g["membros"])
            self.grupos.append(g)
        self._cache = {}

    @staticmethod
    def rotulo_membro(m):
        return f"{m['fluxo']} #{m['idx']} ({m['papel']})"

    def casar(self, ch):
        """Membros (nós) cujo texto casa com a chave; o casamento mais específico vence."""
        if ch in self._cache:
            return self._cache[ch]
        alvo = ch + " "
        melhores, best = [], -1
        for g in self.grupos:
            if g["rx"].match(alvo):
                if g["espec"] > best:
                    best, melhores = g["espec"], [g]
                elif g["espec"] == best:
                    melhores.append(g)
        membros = [m for g in melhores for m in g["membros"]]
        if any(m["alcancavel"] for m in membros):
            membros = [m for m in membros if m["alcancavel"]]
        self._cache[ch] = membros
        return membros

    def resumo(self):
        return {
            "origem": self.origem,
            "nos_com_texto": sum(len(g["membros"]) for g in self.grupos),
            "textos_distintos": len(self.grupos),
            "alcancaveis": sum(1 for g in self.grupos for m in g["membros"] if m["alcancavel"]),
        }


def resolver_no(membros, fluxos_ultima_pergunta):
    """Escolhe o nó quando o mesmo texto existe em vários (ex.: 'Opção inválida!
    Escolha uma das opções.' é erro de 02, Financeiro e Intermediário): usa o
    fluxo da última pergunta que mandamos na conversa."""
    if not membros:
        return None, (), ()
    if len(membros) > 1 and fluxos_ultima_pergunta:
        f = [m for m in membros if m["fluxo"] in fluxos_ultima_pergunta]
        if f:
            membros = f
    rot = [Catalogo.rotulo_membro(m) for m in membros]
    rot = sorted(dict.fromkeys(rot))
    nome = rot[0] if len(rot) == 1 else (" | ".join(rot) if len(rot) <= 3 else f"{rot[0]} (+{len(rot) - 1} nós)")
    return nome, tuple(sorted({m["fluxo"] for m in membros})), tuple(sorted({m["papel"] for m in membros}))


# =============================================================================
# 4. Classificação por texto (sem contexto)
# =============================================================================
CAT_FLUXO = "fluxo"
CAT_INTEG = "integracao"
CAT_INAT = "inatividade"
CAT_PESQ = "pesquisa"
CAT_HUM = "humano"
CAT_TPL = "template_disparo"
CAT_SIS = "sistema"
CAT_AVISO = "aviso_fora_de_fluxo"
CAT_NC = "nao_classificada"
CATEGORIAS = [CAT_FLUXO, CAT_INTEG, CAT_INAT, CAT_PESQ, CAT_HUM, CAT_TPL, CAT_SIS, CAT_AVISO, CAT_NC]


def classificar_texto(masc, ch, catalogo):
    """(categoria, subtipo, membros_do_catalogo, fonte). Só texto — o contexto
    (campo de atendente, fora da janela, início da conversa) entra depois."""
    b = busca(masc)
    for sub, rx in INATIVIDADE:
        if rx.search(b):
            return CAT_INAT, sub, [], "texto_padrao"
    for sub, rx in PESQUISA:
        if rx.search(b):
            return CAT_PESQ, sub, catalogo.casar(ch), "texto_padrao"
    for sub, rx in INTEGRACAO_FORTE:
        if rx.search(b):
            return CAT_INTEG, sub, [], "texto_padrao"
    if b.strip(" !.") == "opcao invalida":
        # É o texto da integração de fatura (par 21x com "não consegui encontrar
        # sua fatura"); o nó "Falar com atendentes" tem o mesmo texto, mas é
        # inalcançável.
        return CAT_INTEG, "opcao_invalida_integracao", [], "texto_padrao"
    membros = catalogo.casar(ch)
    if membros:
        papeis = {m["papel"] for m in membros}
        cat = CAT_PESQ if papeis == {"pesquisa"} else CAT_FLUXO
        return cat, "/".join(sorted(papeis)), membros, "texto_no"
    for sub, rx in SISTEMA:
        if rx.search(b):
            return CAT_SIS, sub, [], "texto_padrao"
    for sub, rx in HUMANO_PADROES:
        if rx.search(b):
            return CAT_HUM, sub, [], "texto_padrao"
    for sub, rx in AVISO_FORA_FLUXO:
        if rx.search(b):
            return CAT_AVISO, sub, [], "texto_padrao"
    for sub, rx in COBRANCA:
        if rx.search(b):
            return CAT_TPL, sub, [], "texto_padrao"
    for sub, rx in INTEGRACAO_HEUR:
        if rx.search(b):
            return CAT_INTEG, sub, [], "texto_padrao_heuristico"
    return CAT_NC, None, [], None


# =============================================================================
# 5. Utilidades de tempo e estatística
# =============================================================================
def parse_dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def dia_sp(dt):
    if dt is None:
        return None
    return (dt.astimezone(TZ_SP) if TZ_SP else dt).strftime("%Y-%m-%d")


def gap_s(a, b):
    if a is None or b is None or a.get("dt") is None or b.get("dt") is None:
        return None
    return (b["dt"] - a["dt"]).total_seconds()


FAIXAS = ["<5s", "5-60s", "1-10min", ">10min", "sem_data"]


def faixa(g):
    if g is None:
        return "sem_data"
    if g < 5:
        return "<5s"
    if g < 60:
        return "5-60s"
    if g < 600:
        return "1-10min"
    return ">10min"


def quantis(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return None

    def q(p):
        k = (len(v) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)

    return {"n": len(v), "p10": q(0.10), "p25": q(0.25), "p50": q(0.50), "p75": q(0.75), "p90": q(0.90)}


def dist_faixas(vals):
    c = Counter(faixa(g) for g in vals)
    return {f: c.get(f, 0) for f in FAIXAS}


def minutos_mais_comuns(vals, n=8):
    c = Counter(int(round(g / 60.0)) for g in vals if g is not None and 0 <= g < 7200)
    return [{"minutos": k, "n": v} for k, v in c.most_common(n)]


# =============================================================================
# 6. Preparação da amostra
# =============================================================================
CAMPOS_CONHECIDOS = {
    "_id", "id", "id_rota", "mensagem", "tipo", "tipoDestinatario", "tipo_destinatario",
    "canalComunicacao", "canal_comunicacao", "envioForaJanela24h", "envio_fora_janela_24h",
    "data", "__v", "createdAt", "updatedAt", "_ordem",
}
RX_CAMPO_IMPRIMIVEL = re.compile(
    r"^(tipo\w*|status\w*|origem|canal|fonte|source|type|kind|categoria|evento|acao|"
    r"template\w*|hsm|bot|chatbot|automatic\w*|automatico|is_\w+|eh_\w+|tipoRemetente|tipo_remetente|"
    r"lida|lido|entregue|enviada|enviado)$", re.I)
RX_VALOR_IMPRIMIVEL = re.compile(r"^[\w\-. ]{1,30}$")
RX_CAMPO_DEST = re.compile(r"destinat|contato|cliente|telefone|fone|numero|phone|jid|chat|celular|wa_?id|^to$|^para$", re.I)


def valor_campo(raw, k):
    v = raw.get(k)
    if isinstance(v, dict):
        for kk in ("_id", "id", "telefone", "numero", "phone", "nome"):
            if v.get(kk) not in (None, ""):
                return str(v.get(kk))
        return None
    if isinstance(v, list):
        return f"<lista:{len(v)}>" if v else None
    if v in (None, "", False) and not isinstance(v, bool):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def forma_valor(v):
    if v is None:
        return "nulo"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "numero"
    if isinstance(v, dict):
        return "dict[" + ",".join(sorted(str(k) for k in v.keys())[:12]) + "]"
    if isinstance(v, list):
        return "lista"
    s = str(v)
    if re.fullmatch(r"[0-9a-f]{24}", s):
        return "objectid"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", s):
        return "data_iso"
    return "texto_curto" if len(s) <= 30 else "texto_longo"


def preparar(raw, catalogo):
    td = str(raw.get("tipoDestinatario") or raw.get("tipo_destinatario") or "").lower()
    d = "env" if td == "clientes_users" else ("rec" if td == "usuarios" else "outro")
    fj = raw.get("envioForaJanela24h", raw.get("envio_fora_janela_24h"))
    m = {
        "id": str(raw.get("_id") or raw.get("id") or ""),
        "rota": str(raw.get("id_rota") or "") or "?",
        "dir": d,
        "dt": parse_dt(raw.get("data")),
        "ordem": raw.get("_ordem", 0),
        "fj": fj if isinstance(fj, bool) else None,
        "tipo": str(raw.get("tipo") or ""),
    }
    if d != "env":
        return m
    txt, forma, chaves_dict, tpl_dict = texto_de(raw.get("mensagem"))
    masc = mascarar(txt)
    ch = chave(masc)
    cat, sub, membros, fonte = classificar_texto(masc, ch, catalogo)
    tpl_tipo = bool(re.search(r"template|hsm", m["tipo"], re.I))
    tpl_campos = [k for k in raw.keys() if re.search(r"template|hsm|campanha|disparo", str(k), re.I)
                  and valor_campo(raw, k) not in (None, "false")]
    m.update({
        "raw": raw, "masc": masc, "chave": ch, "b": busca(masc), "forma": forma,
        "cat0": cat, "sub0": sub, "membros": membros, "fonte0": fonte,
        "tpl_campo": bool(tpl_dict or tpl_tipo or tpl_campos),
        "tpl_campo_motivo": ("mensagem_dict" if tpl_dict else "tipo" if tpl_tipo else
                             ("campo:" + ",".join(sorted(tpl_campos))) if tpl_campos else None),
    })
    m["tags"] = tags_de(m["b"])
    return m


# =============================================================================
# 7. Calibração: qual campo (se houver) marca mensagem de atendente humano
# =============================================================================
def calibrar_campo_humano(env):
    """Procura, entre os campos que a API devolve, o que separa mensagem de
    atendente (texto 'me chamo…', assinatura, despedida de atendente) de
    mensagem do bot (texto igual a nó de fluxo, pesquisa, inatividade).
    Nada é presumido: se nenhum campo separa, devolve None e diz por quê."""
    bot = [m for m in env if m["cat0"] in (CAT_FLUXO, CAT_PESQ, CAT_INAT)
           or (m["cat0"] == CAT_INTEG and m["fonte0"] == "texto_padrao")]
    hum = [m for m in env if m["cat0"] == CAT_HUM and m["sub0"] in
           ("apresentacao_atendente", "assinatura_atendente", "despedida_atendente")]
    info = {"n_bot_referencia": len(bot), "n_humano_referencia": len(hum), "candidatos": [], "escolhido": None}
    campos = sorted({k for m in env for k in m["raw"].keys()} - CAMPOS_CONHECIDOS)
    if CAMPO_HUMANO_FORCADO:
        info["escolhido"] = {"campo": CAMPO_HUMANO_FORCADO, "regra": "presente", "forcado": True}
        return info
    if len(hum) < 5 or len(bot) < 20:
        info["motivo"] = "referências insuficientes (precisa >=5 msgs de atendente e >=20 de bot reconhecidas pelo texto)"
        return info

    def p(grupo, pred):
        return sum(1 for m in grupo if pred(m)) / len(grupo)

    cands = []
    for k in campos:
        vals_bot = [valor_campo(m["raw"], k) for m in bot]
        vals_all = [valor_campo(m["raw"], k) for m in env]
        distintos = {v for v in vals_all if v is not None}
        if not distintos:
            continue
        regras = [
            ("presente", lambda m, k=k: valor_campo(m["raw"], k) is not None),
            ("ausente", lambda m, k=k: valor_campo(m["raw"], k) is None),
        ]
        cont = Counter(v for v in vals_bot if v is not None)
        if cont:
            dom, n_dom = cont.most_common(1)[0]
            if n_dom / max(len(vals_bot), 1) >= 0.8:
                regras.append(("diferente_do_valor_dominante_do_bot",
                               lambda m, k=k, dom=dom: valor_campo(m["raw"], k) not in (None, dom)))
        if len(distintos) <= 8:
            for j, v in enumerate(sorted(distintos)):
                regras.append((f"igual_a_valor#{j}", lambda m, k=k, v=v: valor_campo(m["raw"], k) == v))
        for nome, pred in regras:
            ph, pb = p(hum, pred), p(bot, pred)
            cands.append({"campo": k, "regra": nome, "p_humano": round(ph, 3), "p_bot": round(pb, 3),
                          "score": round(ph - pb, 3), "_pred": pred,
                          "_valor": (sorted(distintos)[int(nome.split("#")[1])] if nome.startswith("igual_a_valor#") else None)})
    cands.sort(key=lambda c: -c["score"])
    for c in cands[:10]:
        pub = {k: v for k, v in c.items() if not k.startswith("_")}
        if c["_valor"] is not None and RX_CAMPO_IMPRIMIVEL.match(c["campo"]) and RX_VALOR_IMPRIMIVEL.match(c["_valor"]):
            pub["valor"] = c["_valor"]
        info["candidatos"].append(pub)
    if cands and cands[0]["score"] >= 0.6:
        c = cands[0]
        info["escolhido"] = {k: v for k, v in c.items() if not k.startswith("_")}
        info["_pred"] = c["_pred"]
    else:
        info["motivo"] = "nenhum campo separa atendente de bot com score >= 0.6 (P(marca|atendente) - P(marca|bot))"
    return info


def pred_humano(info):
    if info.get("_pred"):
        return info["_pred"]
    esc = info.get("escolhido")
    if esc and esc.get("forcado"):
        k = esc["campo"]
        return lambda m: valor_campo(m["raw"], k) is not None
    return None


# =============================================================================
# 8. Campo de destinatário
# =============================================================================
def detectar_destinatario(conversas_normais):
    """Qual campo identifica o destinatário: presente nas enviadas, ~1 valor por
    conversa, e muitos valores no total (não é o canal)."""
    env_por_conv = {r: [m for m in ms if m["dir"] == "env"] for r, ms in conversas_normais.items()}
    todas = [m for ms in env_por_conv.values() for m in ms]
    info = {"candidatos": [], "escolhido": None}
    if not todas:
        info["motivo"] = "sem mensagens enviadas"
        return info
    campos = sorted({k for m in todas for k in m["raw"].keys()} - CAMPOS_CONHECIDOS)
    campos = [k for k in campos if RX_CAMPO_DEST.search(k)] + [k for k in campos if not RX_CAMPO_DEST.search(k)]
    n_conv = max(sum(1 for ms in env_por_conv.values() if ms), 1)
    for k in campos:
        pres = sum(1 for m in todas if valor_campo(m["raw"], k) is not None) / len(todas)
        if pres < 0.5:
            continue
        por_conv = []
        total = set()
        for ms in env_por_conv.values():
            vs = {valor_campo(m["raw"], k) for m in ms} - {None}
            if vs:
                por_conv.append(len(vs))
                total |= vs
        if not por_conv:
            continue
        frac1 = sum(1 for x in por_conv if x == 1) / len(por_conv)
        c = {"campo": k, "presenca_nas_enviadas": round(pres, 3), "frac_conversas_com_1_valor": round(frac1, 3),
             "valores_distintos_total": len(total), "conversas": n_conv,
             "nome_sugere_destinatario": bool(RX_CAMPO_DEST.search(k))}
        c["qualifica"] = pres >= 0.8 and frac1 >= 0.9 and len(total) >= 0.3 * n_conv
        info["candidatos"].append(c)
    if CAMPO_DESTINATARIO_FORCADO:
        info["escolhido"] = {"campo": CAMPO_DESTINATARIO_FORCADO, "forcado": True}
        return info
    q = [c for c in info["candidatos"] if c["qualifica"]]
    q.sort(key=lambda c: (not c["nome_sugere_destinatario"], -c["presenca_nas_enviadas"], -c["valores_distintos_total"]))
    if q:
        info["escolhido"] = {"campo": q[0]["campo"], "forcado": False}
    else:
        info["motivo"] = "nenhum campo presente em >=80% das enviadas com 1 valor por conversa em >=90% delas"
    info["candidatos"] = info["candidatos"][:12]
    return info


# =============================================================================
# 9. Classificação com contexto (por conversa)
# =============================================================================
def classificar_conversa(msgs, eh_humano, proativa_info):
    """Define m['cat'], m['sub'], m['fonte'], m['no'], m['no_fluxos'], m['papel'].

    Regras, em ordem:
      - `tipo`/campo de template na mensagem → template_disparo (fonte campo);
      - texto não reconhecido (ou aviso fora de fluxo) com envioForaJanela24h=True
        → template_disparo (fonte campo: a Meta só aceita template fora da janela);
      - texto não reconhecido/aviso/padrão de atendente com o campo de atendente
        marcado → humano (fonte campo);
      - texto não reconhecido depois de uma transferência → humano
        (heuristica_pos_transferencia: depois de transferir o bot só fala por
        pesquisa/inatividade, que têm texto conhecido);
      - texto não reconhecido na primeira rajada de conversa que começa com
        mensagem nossa → template_disparo (heuristica_inicio_nosso).
    """
    transferida = False
    fluxos_perg = None
    primeira_rajada = True
    # Só vale se a 1a rajada da AMOSTRA é a 1a da conversa (não cortada pela janela).
    proativa = bool(proativa_info and proativa_info.get("inicio_nosso") and not proativa_info.get("truncada"))
    for m in msgs:
        if m["dir"] == "rec":
            primeira_rajada = False
            continue
        if m["dir"] != "env":
            continue
        cat, sub, fonte = m["cat0"], m["sub0"], m["fonte0"]
        no, no_fluxos, papel = resolver_no(m["membros"], fluxos_perg)
        hum_campo = eh_humano(m) if eh_humano else None
        m["hum_campo"] = hum_campo
        if m["tpl_campo"]:
            cat, fonte = CAT_TPL, "campo:" + (m["tpl_campo_motivo"] or "?")
        elif cat in (CAT_NC, CAT_AVISO) and m["fj"] is True:
            cat, fonte = CAT_TPL, "campo:envioForaJanela24h"
        elif cat in (CAT_NC, CAT_AVISO, CAT_HUM) and hum_campo:
            cat, fonte = CAT_HUM, "campo:atendente"
        elif cat == CAT_NC and transferida:
            cat, fonte = CAT_HUM, "heuristica_pos_transferencia"
        elif cat == CAT_NC and proativa and primeira_rajada:
            cat, fonte = CAT_TPL, "heuristica_inicio_nosso"
        m.update({"cat": cat, "sub": sub, "fonte": fonte, "no": no, "no_fluxos": no_fluxos, "papel": papel})
        if papel and ({"pergunta", "pesquisa"} & set(papel)):
            fluxos_perg = no_fluxos
        if (cat in (CAT_HUM, CAT_SIS) or (cat == CAT_INAT and sub == "transferencia_por_inatividade")
                or "transferencia_para_atendente" in m["tags"]):
            transferida = True


def gid(m):
    """Id de agrupamento: o nó (se casou com um), o subtipo para texto de sistema/
    integração (ex.: 'escolha_titulo' com 1, 2 ou 3 títulos é uma coisa só), senão
    a chave do texto mascarado."""
    if m.get("no"):
        return "no:" + m["no"]
    if m.get("sub") and m["cat"] == m["cat0"] and m["cat"] in (CAT_INTEG, CAT_INAT, CAT_PESQ):
        return f"{m['cat']}:{m['sub']}"
    return f"txt:{m['cat']}:{m['chave']}"


# =============================================================================
# 10. Análise
# =============================================================================
class Registro:
    """Guarda, por id de agrupamento, o exemplo mascarado e as conversas — é daqui
    que sai o rótulo impresso (ou '(texto oculto)')."""

    def __init__(self):
        self.info = {}

    def ver(self, m):
        g = gid(m)
        r = self.info.get(g)
        if r is None:
            r = self.info[g] = {"cat": m["cat"], "sub": m.get("sub"), "no": m.get("no"),
                                "membro": (m.get("membros") or [None])[0],
                                "masc": m["masc"], "conversas": set(), "n": 0}
        r["conversas"].add(m["rota"])
        r["n"] += 1
        return g

    def rotulo(self, g):
        r = self.info.get(g)
        if r is None:
            return g
        return rotulo_seguro(r["cat"], r["sub"], r["no"], r["membro"], r["masc"], len(r["conversas"]))


# Subtipos cujo texto é do sistema/integração (não carrega texto livre de
# atendente): o exemplo mascarado sai sem o filtro de K conversas.
SUB_TEXTO_FIXO = {
    "transferencia_por_inatividade", "sessao_expirou", "pesquisa_1a5", "pesquisa_0a5",
    "pix_cabecalho", "pix_codigo", "escolha_titulo", "opcao_invalida_integracao",
}


def masc_do_no(membro):
    """Texto do NÓ (não o da mensagem) mascarado — é o que se imprime para nó de fluxo."""
    return mascarar(_placeholders(membro["texto"])).replace("{PH}", "{…}")


def rotulo_seguro(cat, sub, no, membro, masc, n_conv):
    if no and membro:
        s = f"[{no}] {rotulo(masc_do_no(membro), 90)}"
    elif sub in SUB_TEXTO_FIXO:
        s = f"[{cat}:{sub}] {rotulo(masc, 90)}"
    else:
        k = K_MIN_HUMANO if cat == CAT_HUM else K_MIN
        cab = f"[{cat}" + (f":{sub}" if sub else "") + "]"
        s = f"{cab} {rotulo(masc, 110)}" if n_conv >= k else f"{cab} (texto oculto: {n_conv} conversa(s) < {k})"
    return s if lgpd_ok(s) else f"[{cat}] (texto oculto pelo filtro LGPD)"


RAMO_ROTULOS = {
    "sem_conexao": "Sem conexão → 'Verifiquei… vou te transferir para o suporte'",
    "massiva": "Massiva → pergunta 'Percebi problema generalizado…'",
    "velocidade_reduzida": "Velocidade reduzida → pergunta de liberação",
    "bloqueado_pergunta_desbloqueio": "Bloqueado → PIX → pergunta de desbloqueio em confiança",
    "bloqueado_restricao_menu": "Bloqueado c/ restrição → PIX → menu",
    "financeiro_atraso": "Financeiro em atraso → PIX → 'Posso te ajudar…?'",
    "online_menu": "Online → menu principal",
    "erro_diagnostico_menu": "Integração de diagnóstico falhou → menu",
    "falha_fatura": "Bloqueado/atraso → 'Não consegui encontrar sua fatura'",
    "pix_escolha_titulo": "Bloqueado/atraso com vários títulos → 'Escolha uma opção: 1 - título…'",
    "intermediario_sem_pix": "'Posso te ajudar…?' sem PIX antes",
    "indeterminado": "Nenhum marcador de ramo nas mensagens seguintes (janela/cliente falou antes)",
}


def _tem_no(m, fluxo, papel=None):
    if m.get("dir") != "env" or not m.get("no_fluxos"):
        return False
    if fluxo not in m["no_fluxos"]:
        return False
    return papel is None or papel in (m.get("papel") or ())


def detectar_ramo(msgs, i):
    """Ramo do '01 - Diagnóstico' pela primeira mensagem-marcador depois da saudação
    de cliente (olha até 12 mensagens / 15 min adiante)."""
    viu_pix = False
    viu_limite_diag = False
    t0 = msgs[i]["dt"]
    for m in msgs[i + 1: i + 13]:
        if t0 and m["dt"] and (m["dt"] - t0).total_seconds() > 900:
            break
        if m["dir"] != "env":
            continue
        if m.get("cat") == CAT_INTEG and m.get("sub") in ("pix_cabecalho", "pix_codigo"):
            viu_pix = True
            continue
        if m.get("cat") == CAT_INTEG and m.get("sub") == "escolha_titulo":
            return "pix_escolha_titulo"
        if _tem_no(m, "01 - Diagnóstico de Contrato", "msg"):
            return "sem_conexao"
        if _tem_no(m, "01 - Diagnóstico de Contrato", "limite"):
            if "fatura" in m["chave"]:
                return "falha_fatura"
            viu_limite_diag = True
            continue
        if _tem_no(m, "Massiva", "pergunta"):
            return "massiva"
        if _tem_no(m, "Pergunta, Liberação de Redução", "pergunta"):
            return "velocidade_reduzida"
        if _tem_no(m, "Pergunta, Desbloqueio em Confiança", "pergunta"):
            return "bloqueado_pergunta_desbloqueio"
        if _tem_no(m, "Intermediário", "pergunta"):
            return "financeiro_atraso" if viu_pix else "intermediario_sem_pix"
        if _tem_no(m, "02 - Principal Entrada", "pergunta"):
            if viu_pix:
                return "bloqueado_restricao_menu"
            return "erro_diagnostico_menu" if viu_limite_diag else "online_menu"
    return "indeterminado"


def env_ate_cliente(msgs, i):
    n = 0
    for m in msgs[i:]:
        if m["dir"] == "rec":
            return n
        if m["dir"] == "env":
            n += 1
    return n


def analisar(raws, catalogo, buscar_rota=None):
    t_ini = time.time()
    R = {"versao": VERSAO, "gerado_em": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "parametros": {"PAGINAS": PAGINAS, "PAGE": PAGE, "LIMIAR_GIGANTE": LIMIAR_GIGANTE,
                        "LIMIAR_SINCRONO_S": LIMIAR_SINCRONO_S, "VERIFICAR_INICIO": VERIFICAR_INICIO,
                        "MAX_VERIFICACOES": MAX_VERIFICACOES, "K_MIN": K_MIN, "K_MIN_HUMANO": K_MIN_HUMANO,
                        "TOP_N": TOP_N},
         "catalogo_nos": catalogo.resumo(), "notas": []}

    # ---------------------------------------------------------------- esquema
    chaves_dir = defaultdict(Counter)
    tipo_dir = defaultdict(Counter)
    fj_dir = defaultdict(Counter)
    forma_msg = defaultdict(Counter)
    subchaves_dict = Counter()
    formas_campo = defaultdict(Counter)
    valores_campo = defaultdict(Counter)
    for raw in raws:
        td = str(raw.get("tipoDestinatario") or "").lower()
        d = "enviada" if td == "clientes_users" else ("recebida" if td == "usuarios" else f"outro:{td or 'vazio'}")
        for k, v in raw.items():
            if k == "_ordem":
                continue
            chaves_dir[d][k] += 1
            if k not in ("mensagem",):
                formas_campo[k][forma_valor(v)] += 1
                if RX_CAMPO_IMPRIMIVEL.match(k) and isinstance(v, (str, bool, int)) and RX_VALOR_IMPRIMIVEL.match(str(v)):
                    valores_campo[k][f"{d}={v}"] += 1
        tv = str(raw.get("tipo") or "(vazio)")[:30]
        tipo_dir[d][tv if RX_VALOR_IMPRIMIVEL.match(tv) or tv == "(vazio)" else "(outro)"] += 1
        fjv = raw.get("envioForaJanela24h")
        fj_dir[d]["true" if fjv is True else "false" if fjv is False else "ausente"] += 1
        mv = raw.get("mensagem")
        forma_msg[d][type(mv).__name__] += 1
        if isinstance(mv, dict):
            for k in mv.keys():
                subchaves_dict[f"{d}:{k}"] += 1
    R["esquema"] = {
        "chaves_por_direcao": {d: dict(c.most_common()) for d, c in chaves_dir.items()},
        "forma_dos_campos": {k: dict(c.most_common(6)) for k, c in sorted(formas_campo.items())},
        "valores_de_campos_de_tipo_status": {k: dict(c.most_common(20)) for k, c in sorted(valores_campo.items())},
        "tipo_por_direcao": {d: dict(c.most_common(20)) for d, c in tipo_dir.items()},
        "envioForaJanela24h_por_direcao": {d: dict(c) for d, c in fj_dir.items()},
        "mensagem_python_type_por_direcao": {d: dict(c) for d, c in forma_msg.items()},
        "subchaves_de_mensagem_dict": dict(subchaves_dict.most_common(30)),
    }

    # ------------------------------------------------------- prepara/classifica
    log(f"[v2] classificando {len(raws)} mensagens…")
    msgs_all = [preparar(raw, catalogo) for raw in raws]
    env_all = [m for m in msgs_all if m["dir"] == "env"]
    dias = Counter(dia_sp(m["dt"]) for m in msgs_all if m["dt"])
    dias_env = Counter(dia_sp(m["dt"]) for m in env_all if m["dt"])
    ds = sorted(d for d in dias if d)
    R["janela"] = {
        "mensagens_lidas": len(msgs_all),
        "enviadas": len(env_all),
        "recebidas": sum(1 for m in msgs_all if m["dir"] == "rec"),
        "direcao_desconhecida": sum(1 for m in msgs_all if m["dir"] == "outro"),
        "primeiro_dia": ds[0] if ds else None, "ultimo_dia": ds[-1] if ds else None,
        "dias": len(ds),
        "dias_completos": max(len(ds) - 2, 0),
        "nota_dias": "primeiro e último dia são parciais (a janela é a ponta da coleção)",
        "enviadas_por_dia": {d: dias_env.get(d, 0) for d in ds},
        "enviadas_por_dia_media_dias_completos": (round(sum(dias_env.get(d, 0) for d in ds[1:-1]) / (len(ds) - 2), 1)
                                                  if len(ds) > 2 else None),
    }

    # ---------------------------------------------------------- campo humano
    cal = calibrar_campo_humano(env_all)
    eh_humano = pred_humano(cal)
    R["campo_atendente_humano"] = {k: v for k, v in cal.items() if not k.startswith("_")}
    if eh_humano is None:
        R["notas"].append("Nenhum campo da API identifica mensagem de atendente com segurança; 'humano' vem de "
                          "texto conhecido (me chamo…, despedida, respostas do comercial) e da heurística "
                          "pós-transferência.")

    # --------------------------------------------------------------- conversas
    conversas = defaultdict(list)
    for m in msgs_all:
        conversas[m["rota"]].append(m)
    for ms in conversas.values():
        ms.sort(key=lambda m: (m["dt"] or DT_MIN, m["ordem"]))
    sem_rota = conversas.pop("?", [])
    gigantes = {r: ms for r, ms in conversas.items() if len(ms) > LIMIAR_GIGANTE}
    normais = {r: ms for r, ms in conversas.items() if len(ms) <= LIMIAR_GIGANTE}

    # ------------------------------------------------ início das conversas
    proativa = {}
    candidatas = [r for r, ms in normais.items() if ms and ms[0]["dir"] == "env"]
    verif = {"candidatas_inicio_nosso_na_amostra": len(candidatas), "verificadas": 0, "falhas": 0,
             "inicio_nosso_confirmado": 0, "truncadas_inicio_cliente": 0, "nao_verificadas": 0,
             "api_sem_retorno_para_a_rota": 0}
    for j, r in enumerate(candidatas):
        info = {"inicio_nosso": True, "verificada": False, "primeira_rajada_real": None}
        if buscar_rota is not None and VERIFICAR_INICIO and j < MAX_VERIFICACOES:
            try:
                # Se a API ignorasse o filtro, viriam mensagens de outras conversas:
                # descarta, e a conversa fica como não verificada em vez de errada.
                itens = [x for x in (buscar_rota(r) or []) if str(x.get("id_rota") or "") == r]
                ms_full = sorted((preparar_leve(x, k) for k, x in enumerate(itens)),
                                 key=lambda m: (m["dt"] or DT_MIN, m["ordem"]))
                if ms_full:
                    info["verificada"] = True
                    verif["verificadas"] += 1
                    primeiro_amostra = normais[r][0]
                    info["truncada"] = any(x["id"] != primeiro_amostra["id"] and
                                           (x["dt"] or DT_MIN) < (primeiro_amostra["dt"] or DT_MIN) for x in ms_full)
                    info["inicio_nosso"] = ms_full[0]["dir"] == "env"
                    if info["inicio_nosso"]:
                        verif["inicio_nosso_confirmado"] += 1
                        raj = []
                        for x in ms_full:
                            if x["dir"] == "rec":
                                break
                            if x["dir"] == "env":
                                raj.append(x)
                        info["primeira_rajada_real"] = [(y["id"], y["raw_min"]) for y in raj]
                    else:
                        verif["truncadas_inicio_cliente"] += 1
                else:
                    verif["api_sem_retorno_para_a_rota"] += 1
                if (j + 1) % 50 == 0:
                    log(f"[v2] início conferido em {j + 1}/{min(len(candidatas), MAX_VERIFICACOES)} conversas")
            except Exception as exc:  # não derruba a análise
                verif["falhas"] += 1
                info["erro"] = type(exc).__name__
        else:
            verif["nao_verificadas"] += 1
        proativa[r] = info
    R["verificacao_inicio"] = verif

    for r, ms in list(normais.items()) + list(gigantes.items()):
        classificar_conversa(ms, eh_humano, proativa.get(r))
    classificar_conversa(sem_rota, eh_humano, None)

    reg = Registro()
    for ms in list(normais.values()) + list(gigantes.values()) + [sem_rota]:
        for m in ms:
            if m["dir"] == "env":
                m["gid"] = reg.ver(m)

    # ------------------------------------------------------------- rajadas
    tamanhos = Counter()
    n_rajadas = excesso = conversas_com_rajada = 0
    exc_sinc = exc_assinc = exc_semdata = 0
    exc_assinc_cat = Counter()
    gaps_todos = []
    gaps_por_cat2 = defaultdict(list)
    rajadas_sincronas = rajadas_mistas = 0
    sub_sinc_tam = Counter()
    pares = Counter()
    pares_gap = defaultdict(list)
    trincas = Counter()
    causa = Counter()
    causa_sinc = Counter()
    por_no = defaultdict(lambda: {"enviadas": 0, "excesso": 0, "excesso_sincrono": 0, "excesso_assincrono": 0,
                                  "primeira_da_rajada": 0, "fora_janela": 0, "conversas": set(),
                                  "fluxos": (), "papel": ()})
    por_cat = defaultdict(lambda: {"enviadas": 0, "excesso": 0, "excesso_sincrono": 0, "excesso_assincrono": 0,
                                   "fora_janela": 0, "conversas": set(), "por_fonte": Counter(), "por_subtipo": Counter(),
                                   "por_tipo": Counter()})
    dup_exata_consec = Counter()
    dup_sem_rajada = Counter()
    dup_sem_pares = Counter()
    rep_tag_conv = Counter()
    rep_tag_msgs = Counter()
    rep_tag_entre_origens = Counter()
    rep_chave_conv = Counter()
    rep_chave_msgs = Counter()
    rep_tag_pares_origem = Counter()

    def rel_fluxos(a, b):
        if a["cat"] == CAT_FLUXO and b["cat"] == CAT_FLUXO and a.get("no_fluxos") and b.get("no_fluxos"):
            return "mesmo_fluxo" if set(a["no_fluxos"]) & set(b["no_fluxos"]) else "entre_fluxos"
        return ""

    def fechar(raj, rota):
        nonlocal n_rajadas, excesso, exc_sinc, exc_assinc, exc_semdata, rajadas_sincronas, rajadas_mistas
        if not raj:
            return False
        por_no[raj[0]["gid"]]["primeira_da_rajada"] += 1
        if len(raj) < 2:
            return False
        n_rajadas += 1
        tamanhos[len(raj)] += 1
        excesso += len(raj) - 1
        ids = [m["gid"] for m in raj]
        for i in range(len(ids) - 1):
            pares[(ids[i], ids[i + 1])] += 1
            g = gap_s(raj[i], raj[i + 1])
            if g is not None:
                pares_gap[(ids[i], ids[i + 1])].append(g)
        for i in range(len(ids) - 2):
            trincas[tuple(ids[i:i + 3])] += 1
        mista = False
        bloco = 1
        vistos_tags = {}
        for i, m in enumerate(raj):
            for t in m["tags"]:
                if t in vistos_tags:
                    dup_sem_rajada[t] += 1
                    dup_sem_pares[(t, vistos_tags[t], m["gid"])] += 1
                else:
                    vistos_tags[t] = m["gid"]
            if i == 0:
                continue
            ant = raj[i - 1]
            g = gap_s(ant, m)
            gaps_todos.append(g)
            gaps_por_cat2[m["cat"]].append(g)
            k = f"{ant['cat']}→{m['cat']}"
            rf = rel_fluxos(ant, m)
            if rf:
                k += f" ({rf})"
            causa[k] += 1
            chave_no = m["gid"]
            por_no[chave_no]["excesso"] += 1
            por_cat[m["cat"]]["excesso"] += 1
            if ant["chave"] == m["chave"]:
                dup_exata_consec[m["gid"]] += 1
            if g is None:
                exc_semdata += 1
                bloco += 1
            elif g < LIMIAR_SINCRONO_S:
                exc_sinc += 1
                causa_sinc[k] += 1
                por_no[chave_no]["excesso_sincrono"] += 1
                por_cat[m["cat"]]["excesso_sincrono"] += 1
                bloco += 1
            else:
                mista = True
                exc_assinc += 1
                exc_assinc_cat[m["cat"] + (f":{m['sub']}" if m["cat"] in (CAT_INAT, CAT_PESQ, CAT_SIS) and m.get("sub") else "")] += 1
                por_no[chave_no]["excesso_assincrono"] += 1
                por_cat[m["cat"]]["excesso_assincrono"] += 1
                if bloco >= 1:
                    sub_sinc_tam[bloco] += 1
                bloco = 1
        sub_sinc_tam[bloco] += 1
        if mista:
            rajadas_mistas += 1
        else:
            rajadas_sincronas += 1
        return True

    for rota, ms in normais.items():
        raj = []
        teve = False
        tags_conv = defaultdict(list)
        chaves_conv = Counter()
        for m in ms:
            if m["dir"] == "env":
                raj.append(m)
                chave_no = m["gid"]
                pn = por_no[chave_no]
                pn["enviadas"] += 1
                pn["conversas"].add(rota)
                pn["fluxos"] = m.get("no_fluxos") or ()
                pn["papel"] = m.get("papel") or ()
                if m["fj"] is True:
                    pn["fora_janela"] += 1
                pc = por_cat[m["cat"]]
                pc["enviadas"] += 1
                pc["conversas"].add(rota)
                pc["por_fonte"][m["fonte"] or "texto"] += 1
                pc["por_subtipo"][m.get("sub") or "-"] += 1
                pc["por_tipo"][(m["tipo"] or "(vazio)")[:30]] += 1
                if m["fj"] is True:
                    pc["fora_janela"] += 1
                for t in m["tags"]:
                    tags_conv[t].append(m["gid"])
                chaves_conv[m["gid"]] += 1
            elif m["dir"] == "rec":
                teve |= fechar(raj, rota)
                raj = []
        teve |= fechar(raj, rota)
        conversas_com_rajada += 1 if teve else 0
        for t, origens in tags_conv.items():
            if len(origens) >= 2:
                rep_tag_conv[t] += 1
                rep_tag_msgs[t] += len(origens) - 1
                if len(set(origens)) >= 2:
                    rep_tag_entre_origens[t] += 1
                    ordem = list(dict.fromkeys(origens))
                    rep_tag_pares_origem[(t, ordem[0], ordem[1])] += 1
        for g, c in chaves_conv.items():
            if c >= 2:
                rep_chave_conv[g] += 1
                rep_chave_msgs[g] += c - 1

    env_normais = sum(1 for ms in normais.values() for m in ms if m["dir"] == "env")
    R["conversas"] = {
        "total_na_amostra": len(conversas) + (1 if sem_rota else 0),
        "normais": len(normais),
        "gigantes": len(gigantes),
        "mensagens_sem_id_rota": len(sem_rota),
        "com_rajada": conversas_com_rajada,
    }
    R["totais"] = {
        "enviadas": env_normais,
        "rajadas": n_rajadas,
        "excesso": excesso,
        "excesso_pct_das_enviadas": round(100.0 * excesso / env_normais, 1) if env_normais else None,
        "tamanhos_das_rajadas": {str(k): v for k, v in sorted(tamanhos.items())},
        "definicao": "rajada = mensagens enviadas seguidas na mesma conversa sem mensagem do cliente no meio; "
                     "excesso = mensagens na posição >= 2 da rajada (se cada rajada virasse 1). Gigantes e "
                     "mensagens sem id_rota ficam fora.",
    }
    R["tempo_dentro_da_rajada"] = {
        "limiar_sincrono_s": LIMIAR_SINCRONO_S,
        "intervalos_entre_mensagens_consecutivas": dist_faixas(gaps_todos),
        "intervalos_por_categoria_da_2a_mensagem": {c: dist_faixas(v) for c, v in sorted(gaps_por_cat2.items())},
        "rajadas_sincronas": rajadas_sincronas,
        "rajadas_com_follow_up": rajadas_mistas,
        "excesso_sincrono": exc_sinc,
        "excesso_assincrono": exc_assinc,
        "excesso_sem_data": exc_semdata,
        "excesso_assincrono_por_categoria": dict(exc_assinc_cat.most_common()),
        "blocos_sincronos_por_tamanho": {str(k): v for k, v in sorted(sub_sinc_tam.items())},
        "leitura": "síncrono = a mensagem saiu < limiar depois da anterior (o fluxo mandou de uma vez: juntável "
                   "editando fluxo); assíncrono = saiu depois de silêncio (timeout, pesquisa no fechamento, humano).",
    }

    # -------------------------------------------------------- por categoria/nó
    R["por_categoria"] = {
        c: {"enviadas": v["enviadas"], "excesso": v["excesso"], "excesso_sincrono": v["excesso_sincrono"],
            "excesso_assincrono": v["excesso_assincrono"], "fora_janela_24h": v["fora_janela"],
            "conversas": len(v["conversas"]), "por_fonte": dict(v["por_fonte"].most_common()),
            "por_subtipo": dict(v["por_subtipo"].most_common(15)),
            "por_campo_tipo": dict(v["por_tipo"].most_common(10))}
        for c, v in sorted(por_cat.items(), key=lambda kv: -kv[1]["enviadas"])
    }
    lista_nos = []
    for k, v in por_no.items():
        if not v["enviadas"] and not v["primeira_da_rajada"]:
            continue
        lista_nos.append({
            "no": k[3:] if k.startswith("no:") else None,
            "texto": reg.rotulo(k),
            "fluxos": list(v["fluxos"]), "papel": list(v["papel"]),
            "enviadas": v["enviadas"], "conversas": len(v["conversas"]),
            "excesso": v["excesso"], "excesso_sincrono": v["excesso_sincrono"],
            "excesso_assincrono": v["excesso_assincrono"], "primeira_da_rajada": v["primeira_da_rajada"],
            "fora_janela_24h": v["fora_janela"],
        })
    lista_nos.sort(key=lambda x: (-x["excesso"], -x["enviadas"]))
    R["por_no_e_texto"] = lista_nos[: max(TOP_N * 3, 60)]
    usados = {x["no"] for x in lista_nos if x["no"]}
    vivos = sorted({Catalogo.rotulo_membro(m) for g in catalogo.grupos for m in g["membros"] if m["alcancavel"]})
    R["nos_alcancaveis_sem_envio_na_janela"] = [n for n in vivos if not any(n in u for u in usados)]
    R["por_causa"] = [{"causa": k, "excesso": v, "sincrono": causa_sinc.get(k, 0)} for k, v in causa.most_common()]

    R["pares_top"] = [{"n": c, "a": reg.rotulo(a), "b": reg.rotulo(b),
                       "intervalo_mediano_s": (round(statistics.median(pares_gap[(a, b)]), 1) if pares_gap[(a, b)] else None)}
                      for (a, b), c in pares.most_common(TOP_N)]
    R["trincas_top"] = [{"n": c, "sequencia": [reg.rotulo(x) for x in t]} for t, c in trincas.most_common(max(TOP_N // 2, 10))]

    # ---------------------------------------------------------- duplicidades
    R["duplicidades"] = {
        "mesmo_texto_duas_vezes_seguidas": [{"n": c, "texto": reg.rotulo(g)} for g, c in dup_exata_consec.most_common(TOP_N)],
        "mesmo_assunto_na_mesma_rajada": dict(dup_sem_rajada.most_common()),
        "mesmo_assunto_na_mesma_rajada_origens": [
            {"n": c, "assunto": t, "primeiro": reg.rotulo(a), "repete_em": reg.rotulo(b),
             "entre_fluxos": _entre_fluxos(reg, a, b)}
            for (t, a, b), c in dup_sem_pares.most_common(TOP_N)],
        "assunto_repetido_na_conversa": {
            t: {"conversas": rep_tag_conv[t], "mensagens_repetidas": rep_tag_msgs[t],
                "conversas_com_origens_diferentes": rep_tag_entre_origens[t]}
            for t, _ in rep_tag_conv.most_common()},
        "assunto_repetido_na_conversa_origens": [
            {"n": c, "assunto": t, "primeiro": reg.rotulo(a), "depois": reg.rotulo(b)}
            for (t, a, b), c in rep_tag_pares_origem.most_common(TOP_N)],
        "mesmo_texto_repetido_na_conversa": [
            {"texto": reg.rotulo(g), "conversas": rep_chave_conv[g], "mensagens_repetidas": rep_chave_msgs[g]}
            for g, _ in rep_chave_conv.most_common(TOP_N)],
        "leitura": "assunto = marcador por regex (protocolo, anúncio do App, horário, transferência, avaliação…). "
                   "'mesmo assunto na mesma rajada' = o cliente leu a mesma informação duas vezes sem ter falado.",
    }

    # ------------------------------------------------------------- caminhos
    ramos = defaultdict(lambda: {"ocorrencias": 0, "conversas": set(), "env_ate_cliente_falar": []})
    saud_cli = saud_nao = 0
    conv_saud_cli, conv_saud_nao = set(), set()
    env_ate_nao = []
    for rota, ms in normais.items():
        for i, m in enumerate(ms):
            if m["dir"] != "env":
                continue
            if _tem_no(m, "00 - Verifica se é cliente", "msg"):
                if m["chave"].startswith("seja bem vindo"):
                    saud_nao += 1
                    conv_saud_nao.add(rota)
                    env_ate_nao.append(env_ate_cliente(ms, i))
                else:
                    saud_cli += 1
                    conv_saud_cli.add(rota)
                    rm = detectar_ramo(ms, i)
                    ramos[rm]["ocorrencias"] += 1
                    ramos[rm]["conversas"].add(rota)
                    ramos[rm]["env_ate_cliente_falar"].append(env_ate_cliente(ms, i))
    R["caminhos"] = {
        "saudacao_cliente": {"ocorrencias": saud_cli, "conversas": len(conv_saud_cli)},
        "saudacao_nao_cliente": {"ocorrencias": saud_nao, "conversas": len(conv_saud_nao),
                                 "env_ate_cliente_falar": dict(Counter(env_ate_nao).most_common())},
        "ramos_depois_da_saudacao_de_cliente": {
            k: {"descricao": RAMO_ROTULOS.get(k, k), "ocorrencias": v["ocorrencias"], "conversas": len(v["conversas"]),
                "env_ate_cliente_falar": dict(sorted(Counter(v["env_ate_cliente_falar"]).items()))}
            for k, v in sorted(ramos.items(), key=lambda kv: -kv[1]["ocorrencias"])},
        "leitura": "ramo inferido pela 1a mensagem-marcador depois da saudação (até 12 msgs/15 min). "
                   "env_ate_cliente_falar = quantas mensagens nossas o cliente recebe, contando a própria "
                   "saudação, até ele poder responder (se não respondeu até o fim da janela, conta todas).",
        "conversas_por_no": "ver por_no_e_texto[].conversas",
    }

    # ------------------------------------------------ desfecho de cada pergunta
    # Para cada pergunta/menu/pesquisa que mandamos: o que veio depois. Se o cliente
    # respondeu, é a 1a mensagem nossa depois da resposta (= a opção que ele
    # escolheu, pelo nó que disparou); se falamos de novo sem ele responder, é
    # o follow-up (inatividade, pesquisa, humano…).
    desf = defaultdict(Counter)
    desf_n = Counter()
    for rota, ms in normais.items():
        for i, m in enumerate(ms):
            if m["dir"] != "env" or not ({"pergunta", "pesquisa"} & set(m.get("papel") or ())):
                continue
            resp = False
            out = None
            for x in ms[i + 1:]:
                if x["dir"] == "rec":
                    resp = True
                    continue
                if x["dir"] == "env":
                    out = ("respondeu" if resp else "sem_resposta", x["gid"])
                    break
            if out is None:
                out = ("respondeu_e_nada_mais_na_janela" if resp else "sem_resposta_e_nada_mais_na_janela", None)
            desf[m["gid"]][out] += 1
            desf_n[m["gid"]] += 1
    R["desfecho_das_perguntas"] = [
        {"pergunta": reg.rotulo(g), "vezes": n,
         "depois": [{"n": c, "cliente": k[0], "proxima_nossa": (reg.rotulo(k[1]) if k[1] else None)}
                    for k, c in desf[g].most_common(10)]}
        for g, n in desf_n.most_common(25)]

    # ------------------------------------------------------------ inatividade
    def depois_de(ms, i):
        t0 = ms[i]["dt"]
        prim_hum = prim_cli = None
        prim_sis = None
        for x in ms[i + 1:]:
            if x["dir"] == "rec" and prim_cli is None:
                prim_cli = x
            if x["dir"] == "env" and x.get("cat") == CAT_HUM and prim_hum is None:
                prim_hum = x
            if x["dir"] == "env" and x.get("cat") == CAT_SIS and prim_sis is None:
                prim_sis = x
        return prim_hum, prim_cli, prim_sis, t0

    inat = {}
    for sub in ("transferencia_por_inatividade", "sessao_expirou", "pesquisa_encerrada_inatividade"):
        inat[sub] = {"total": 0, "conversas": set(), "anterior": Counter(), "anterior_foi_cliente": 0,
                     "ultima_nossa_antes": Counter(),
                     "gaps": [], "humano_depois": 0, "sistema_depois": 0, "cliente_voltou": 0,
                     "cliente_voltou_antes_do_humano": 0, "nada_depois": 0, "t_ate_humano": []}
    for rota, ms in normais.items():
        for i, m in enumerate(ms):
            if m["dir"] != "env" or m.get("cat") != CAT_INAT or m.get("sub") not in inat:
                continue
            I = inat[m["sub"]]
            I["total"] += 1
            I["conversas"].add(rota)
            ant = ms[i - 1] if i > 0 else None
            if ant is None:
                I["anterior"]["(início da janela)"] += 1
            elif ant["dir"] == "rec":
                I["anterior_foi_cliente"] += 1
                I["anterior"]["(mensagem do cliente)"] += 1
            else:
                I["anterior"][ant["gid"]] += 1
            I["gaps"].append(gap_s(ant, m) if ant else None)
            ult = next((x for x in reversed(ms[:i]) if x["dir"] == "env"), None)
            I["ultima_nossa_antes"][ult["gid"] if ult else "(nenhuma na janela)"] += 1
            ph, pc, ps, t0 = depois_de(ms, i)
            if ph:
                I["humano_depois"] += 1
                g = gap_s(m, ph)
                if g is not None:
                    I["t_ate_humano"].append(g)
            if ps:
                I["sistema_depois"] += 1
            if pc:
                I["cliente_voltou"] += 1
                if ph is None or (pc["dt"] or DT_MIN) <= (ph["dt"] or DT_MIN):
                    I["cliente_voltou_antes_do_humano"] += 1
            if not (ph or pc or ps) and not any(x["dir"] == "env" for x in ms[i + 1:]):
                I["nada_depois"] += 1
    R["inatividade"] = {}
    for sub, I in inat.items():
        R["inatividade"][sub] = {
            "total": I["total"], "conversas": len(I["conversas"]),
            "mensagem_anterior": [{"n": c, "anterior": (reg.rotulo(g) if not g.startswith("(") else g)}
                                  for g, c in I["anterior"].most_common(TOP_N)],
            "anterior_foi_cliente": I["anterior_foi_cliente"],
            "ultima_mensagem_nossa_antes": [{"n": c, "mensagem": (reg.rotulo(g) if not g.startswith("(") else g)}
                                            for g, c in I["ultima_nossa_antes"].most_common(TOP_N)],
            "tempo_desde_a_mensagem_anterior_s": quantis(I["gaps"]),
            "tempo_desde_a_mensagem_anterior_faixas": dist_faixas(I["gaps"]),
            "tempo_desde_a_mensagem_anterior_minutos_mais_comuns": minutos_mais_comuns(I["gaps"]),
            "depois": {"mensagem_de_humano": I["humano_depois"], "evento_de_sistema": I["sistema_depois"],
                       "cliente_voltou_a_falar": I["cliente_voltou"],
                       "cliente_voltou_antes_do_humano": I["cliente_voltou_antes_do_humano"],
                       "nada_mais_na_conversa": I["nada_depois"]},
            "tempo_ate_mensagem_de_humano_s": quantis(I["t_ate_humano"]),
        }
    R["inatividade"]["leitura"] = ("o tempo entre a mensagem anterior e o aviso estima o 'Tempo máximo de "
                                   "inatividade' configurado no Agente Virtual (valor não legível pela API). "
                                   "'humano' depende do método de R.campo_atendente_humano; 'nada' pode ser "
                                   "fim da janela.")

    # ------------------------------------------------------------- proativas
    R["proativas"] = _proativas(normais, proativa, reg, catalogo, eh_humano)

    # ------------------------------------------------------------- gigantes
    dest = detectar_destinatario(normais)
    R["campo_destinatario"] = dest
    campo_dest = (dest.get("escolhido") or {}).get("campo")
    cands_dest = [c["campo"] for c in dest.get("candidatos", [])][:8]
    if campo_dest and campo_dest not in cands_dest:
        cands_dest.insert(0, campo_dest)
    if not cands_dest:
        todos = sorted({k for ms in gigantes.values() for m in ms if m["dir"] == "env" for k in m["raw"].keys()}
                       - CAMPOS_CONHECIDOS)
        cands_dest = [k for k in todos if RX_CAMPO_DEST.search(k)][:8]
    R["gigantes"] = []
    grupos_g = list(sorted(gigantes.items(), key=lambda kv: -len(kv[1])))
    if sem_rota:
        grupos_g.append(("?", sem_rota))
    for n_g, (rota, ms) in enumerate(grupos_g, 1):
        R["gigantes"].append(_gigante(n_g, rota, ms, reg, campo_dest, cands_dest))

    # ----------------------------------------------------- fora da janela
    fj = [m for ms in normais.values() for m in ms if m["dir"] == "env" and m["fj"] is True]
    fj_g = [m for ms in list(gigantes.values()) + [sem_rota] for m in ms if m["dir"] == "env" and m["fj"] is True]
    cfj = Counter(m["gid"] for m in fj)
    R["fora_da_janela_24h"] = {
        "campo": "envioForaJanela24h (MEDIDO: é o que a Meta cobra como template)",
        "enviadas_com_true_normais": len(fj),
        "enviadas_com_true_gigantes_e_sem_rota": len(fj_g),
        "enviadas_com_false": sum(1 for m in env_all if m["fj"] is False),
        "enviadas_sem_o_campo": sum(1 for m in env_all if m["fj"] is None),
        "por_categoria": dict(Counter(m["cat"] for m in fj + fj_g).most_common()),
        "textos_top": [{"n": c, "texto": reg.rotulo(g)} for g, c in cfj.most_common(TOP_N)],
    }

    # ---------------------------------------------------------- não classificadas
    nc = Counter(m["gid"] for ms in normais.values() for m in ms if m["dir"] == "env" and m["cat"] == CAT_NC)
    R["nao_classificadas_top"] = [{"n": c, "conversas": len(reg.info[g]["conversas"]), "texto": reg.rotulo(g)}
                                  for g, c in nc.most_common(TOP_N)]

    # ------------------------------------------------ conferência com o v1
    R["comparacao_v1"] = _v1_style(conversas, sem_rota)
    R["notas"] += [
        "Tudo aqui é contagem MEDIDA na amostra; a ORIGEM de cada mensagem traz a fonte da decisão "
        "(campo da API, texto igual a nó, texto conhecido fora de fluxo, ou heurística de contexto).",
        "Janela = ponta da coleção. Conversa aberta antes dela aparece cortada: rajada na borda fica menor "
        "e caminho sem saudação não entra em 'caminhos'.",
        "Excesso é TETO, não meta: código PIX separado e aviso de transferência são necessários.",
        "Evento 'X alterou o departamento' conta como enviada porque vem com tipoDestinatario=clientes_users; "
        "se ele chega ao WhatsApp do cliente não dá para saber pela API (ver por_categoria.sistema.por_campo_tipo).",
        "Nó com ' | ' no nome = o mesmo texto existe em vários nós; quando dá, a última pergunta da conversa "
        "desempata.",
    ]
    R["duracao_analise_s"] = round(time.time() - t_ini, 1)
    return R


def _entre_fluxos(reg, a, b):
    ia, ib = reg.info.get(a), reg.info.get(b)
    if not ia or not ib or not ia.get("no") or not ib.get("no"):
        return None
    fa = {s.split(" #")[0] for s in ia["no"].split(" | ")}
    fb = {s.split(" #")[0] for s in ib["no"].split(" | ")}
    return not (fa & fb)


def preparar_leve(raw, k):
    """Versão mínima para conferir o início de uma conversa: direção, data e — só
    nas enviadas — o texto mascarado (recebida nunca tem texto guardado)."""
    td = str(raw.get("tipoDestinatario") or "").lower()
    d = "env" if td == "clientes_users" else ("rec" if td == "usuarios" else "outro")
    m = {"id": str(raw.get("_id") or ""), "dir": d, "dt": parse_dt(raw.get("data")), "ordem": k, "raw_min": None}
    if d == "env":
        txt, _, chaves_dict, tpl = texto_de(raw.get("mensagem"))
        fjv = raw.get("envioForaJanela24h")
        m["raw_min"] = {"masc": mascarar(txt), "fj": fjv if isinstance(fjv, bool) else None,
                        "tpl": bool(tpl or re.search(r"template|hsm", str(raw.get("tipo") or ""), re.I))}
    return m


def _proativas(normais, proativa, reg, catalogo, eh_humano):
    out = {"conversas_comecando_por_nos_na_amostra": 0, "confirmadas_pela_api": 0,
           "confirmadas_mas_iniciadas_antes_da_janela": 0,
           "cortadas_pela_janela": 0, "nao_verificadas": 0,
           "primeira_rajada_tamanho": Counter(), "primeira_mensagem_categoria": Counter(),
           "primeira_mensagem_fora_janela": Counter(), "cliente_respondeu_na_amostra": 0,
           "primeira_rajada_com_texto_repetido": 0}
    textos = defaultdict(set)
    masc_ex = {}
    cat_de = {}
    for r, info in proativa.items():
        ms = normais.get(r) or []
        out["conversas_comecando_por_nos_na_amostra"] += 1
        if info.get("verificada") and not info.get("inicio_nosso"):
            out["cortadas_pela_janela"] += 1
            continue
        if info.get("verificada"):
            out["confirmadas_pela_api"] += 1
            if info.get("truncada"):
                out["confirmadas_mas_iniciadas_antes_da_janela"] += 1
        else:
            out["nao_verificadas"] += 1
        if info.get("primeira_rajada_real"):
            raj = [x for _, x in info["primeira_rajada_real"] if x]
            itens = [(x["masc"], x["fj"], x["tpl"]) for x in raj]
        else:
            itens = []
            for m in ms:
                if m["dir"] == "rec":
                    break
                if m["dir"] == "env":
                    itens.append((m["masc"], m["fj"], m["tpl_campo"]))
        if not itens:
            continue
        out["primeira_rajada_tamanho"][len(itens)] += 1
        chs = []
        for j, (masc, fjv, tpl) in enumerate(itens):
            ch = chave(masc)
            chs.append(ch)
            cat, sub, membros, _ = classificar_texto(masc, ch, catalogo)
            if tpl or (cat in (CAT_NC, CAT_AVISO) and fjv is True):
                cat = CAT_TPL
            elif cat == CAT_NC:
                cat = CAT_TPL + "(heuristica_inicio_nosso)"
            k = (cat, sub, ch)
            textos[k].add(r)
            masc_ex.setdefault(k, masc)
            cat_de[k] = (cat, membros)
            if j == 0:
                out["primeira_mensagem_categoria"][cat] += 1
                out["primeira_mensagem_fora_janela"]["true" if fjv is True else "false" if fjv is False else "ausente"] += 1
        if len(set(chs)) < len(chs):
            out["primeira_rajada_com_texto_repetido"] += 1
        if any(m["dir"] == "rec" for m in ms):
            out["cliente_respondeu_na_amostra"] += 1
    top = []
    for k, rotas in sorted(textos.items(), key=lambda kv: -len(kv[1]))[:TOP_N]:
        cat, membros = cat_de[k]
        no, _, _ = resolver_no(membros, None)
        s = rotulo_seguro(cat, k[1], no, (membros or [None])[0], masc_ex[k], len(rotas))
        top.append({"conversas": len(rotas), "texto": s})
    for c in ("primeira_rajada_tamanho", "primeira_mensagem_categoria", "primeira_mensagem_fora_janela"):
        out[c] = {str(k): v for k, v in sorted(out[c].items(), key=lambda kv: -kv[1])}
    out["textos_da_primeira_rajada_top"] = top
    out["leitura"] = ("proxy de template/disparo proativo: conversa cuja 1a mensagem é nossa. 'cortadas' = a "
                      "API mostrou mensagens anteriores à janela com o cliente falando primeiro (não é proativa).")
    return out


def _gigante(n_g, rota, ms, reg, campo_dest, cands_dest):
    env = [m for m in ms if m["dir"] == "env"]
    dias = sorted({dia_sp(m["dt"]) for m in ms if m["dt"]})
    g = {"id": f"G{n_g}" + (" (mensagens SEM id_rota)" if rota == "?" else ""),
         "mensagens": len(ms), "enviadas": len(env), "recebidas": sum(1 for m in ms if m["dir"] == "rec"),
         "dias": len(dias), "de": dias[0] if dias else None, "ate": dias[-1] if dias else None,
         "categorias": dict(Counter(m.get("cat") for m in env).most_common()),
         "fora_janela_24h_true": sum(1 for m in env if m["fj"] is True),
         "destinatarios_distintos_por_campo": {}}
    for k in cands_dest:
        vs = {valor_campo(m["raw"], k) for m in env} - {None}
        g["destinatarios_distintos_por_campo"][k] = {"distintos": len(vs),
                                                    "presenca": round(sum(1 for m in env if valor_campo(m["raw"], k) is not None) / max(len(env), 1), 3)}
    maior = cur = 0
    gaps = []
    ant = None
    for m in ms:
        if m["dir"] == "env":
            cur += 1
            if ant is not None and ant["dir"] == "env":
                gaps.append(gap_s(ant, m))
            maior = max(maior, cur)
        elif m["dir"] == "rec":
            cur = 0
        ant = m
    g["maior_rajada"] = maior
    g["intervalos_entre_enviadas_seguidas"] = dist_faixas(gaps)
    cont = Counter(m["gid"] for m in env)
    tops = []
    for gi, c in cont.most_common(3):
        item = {"vezes": c, "categoria": reg.info[gi]["cat"], "conversas_na_amostra_com_esse_texto": len(reg.info[gi]["conversas"])}
        rep = [m for m in env if m["gid"] == gi]
        if campo_dest:
            por_dest = Counter(valor_campo(m["raw"], campo_dest) for m in rep)
            por_dest.pop(None, None)
            item["campo_destinatario"] = campo_dest
            item["destinatarios_distintos"] = len(por_dest)
            item["max_vezes_para_um_mesmo_destinatario"] = max(por_dest.values()) if por_dest else None
            item["destinatarios_que_receberam_2_ou_mais"] = sum(1 for v in por_dest.values() if v >= 2)
            consec = 0
            ultimo = {}
            for m in ms:
                if m["dir"] != "env":
                    continue
                dv = valor_campo(m["raw"], campo_dest)
                if m["gid"] == gi and ultimo.get(dv) == gi:
                    consec += 1
                ultimo[dv] = m["gid"]
            item["repeticoes_seguidas_para_o_mesmo_destinatario"] = consec
        else:
            item["destinatarios_distintos"] = None
            item["motivo"] = "campo de destinatário não identificado (ver R.campo_destinatario)"
        seguro = reg.rotulo(gi)
        if (item.get("destinatarios_distintos") or 0) >= 3 and "texto oculto" in seguro and reg.info[gi]["cat"] != CAT_HUM:
            s2 = f"[{reg.info[gi]['cat']}] {rotulo(reg.info[gi]['masc'], 110)}"
            seguro = s2 if lgpd_ok(s2) else seguro
        item["texto"] = seguro
        tops.append(item)
    g["mais_repetidas"] = tops
    return g


def _v1_style(conversas, sem_rota):
    """Mesma conta do v1 (gigantes > 100 fora; qualquer não-'clientes_users' fecha a
    rajada; mensagens sem id_rota viram uma conversa '?'), para comparar números."""
    todas = dict(conversas)
    if sem_rota:
        todas["?"] = sem_rota
    norm = {r: ms for r, ms in todas.items() if len(ms) <= 100}
    env = raj = exc = com = 0
    for ms in norm.values():
        cor = 0
        teve = False
        for m in ms:
            if m["dir"] == "env":
                env += 1
                cor += 1
                continue
            if cor >= 2:
                raj += 1
                exc += cor - 1
                teve = True
            cor = 0
        if cor >= 2:
            raj += 1
            exc += cor - 1
            teve = True
        com += 1 if teve else 0
    return {"conversas": len(norm), "gigantes": len(todas) - len(norm), "enviadas": env, "rajadas": raj,
            "excesso": exc, "conversas_com_rajada": com}


# =============================================================================
# 11. Resumo legível
# =============================================================================
def imprimir_resumo(R):
    p = print
    J, T, C = R["janela"], R["totais"], R["conversas"]
    p("=" * 78)
    p(f"RAJADAS REAIS {R['versao']} — catálogo de nós: {R['catalogo_nos']['origem']}")
    p("=" * 78)
    p(f"Janela: {J['primeiro_dia']} a {J['ultimo_dia']} ({J['dias']} dias, {J['dias_completos']} completos) · "
      f"{J['mensagens_lidas']} mensagens · {J['enviadas']} enviadas · {J['recebidas']} recebidas · "
      f"{J['direcao_desconhecida']} sem direção")
    p(f"Conversas: {C['normais']} normais · {C['gigantes']} gigantes (>{LIMIAR_GIGANTE}) · "
      f"{C['mensagens_sem_id_rota']} msgs sem id_rota · {C['com_rajada']} com rajada")
    p(f"Enviadas (normais) {T['enviadas']} · rajadas {T['rajadas']} · excesso {T['excesso']} "
      f"({T['excesso_pct_das_enviadas']}%)")
    t = R["tempo_dentro_da_rajada"]
    p(f"  excesso síncrono (<{LIMIAR_SINCRONO_S}s) {t['excesso_sincrono']} · assíncrono {t['excesso_assincrono']} · "
      f"sem data {t['excesso_sem_data']}")
    p(f"  intervalos: {t['intervalos_entre_mensagens_consecutivas']}")
    p(f"  assíncrono por categoria: {t['excesso_assincrono_por_categoria']}")
    v1 = R["comparacao_v1"]
    p(f"Conta do v1 na mesma janela: enviadas {v1['enviadas']} · rajadas {v1['rajadas']} · excesso {v1['excesso']}")
    ch = R["campo_atendente_humano"]
    p(f"\nCampo de atendente: {ch.get('escolhido') or ('nenhum — ' + str(ch.get('motivo')))}")
    cd = R["campo_destinatario"]
    p(f"Campo de destinatário: {cd.get('escolhido') or ('nenhum — ' + str(cd.get('motivo')))}")
    fj = R["fora_da_janela_24h"]
    p(f"Fora da janela 24h (template): normais {fj['enviadas_com_true_normais']} · gigantes/sem rota "
      f"{fj['enviadas_com_true_gigantes_e_sem_rota']} · false {fj['enviadas_com_false']} · sem campo "
      f"{fj['enviadas_sem_o_campo']}")

    p("\nPOR CATEGORIA (enviadas · excesso [sínc/assínc] · conversas · fora janela)")
    for c, v in R["por_categoria"].items():
        p(f"  {c:<22} {v['enviadas']:>6} · {v['excesso']:>6} [{v['excesso_sincrono']}/{v['excesso_assincrono']}] · "
          f"{v['conversas']:>5} · {v['fora_janela_24h']}   fontes={v['por_fonte']}")

    p("\nPOR CAUSA DO EXCESSO (anterior → atual)")
    for x in R["por_causa"][:20]:
        p(f"  {x['excesso']:>6} (sínc {x['sincrono']:>5})  {x['causa']}")

    p("\nNÓS/TEXTOS COM MAIS EXCESSO (excesso · enviadas · conversas)")
    for x in R["por_no_e_texto"][:TOP_N]:
        p(f"  {x['excesso']:>5} · {x['enviadas']:>5} · {x['conversas']:>4}  {x['texto'][:120]}")

    p("\nO QUE VEM DEPOIS DE CADA PERGUNTA (top 8 perguntas, 5 desfechos)")
    for q in R["desfecho_das_perguntas"][:8]:
        p(f"  {q['vezes']:>5}x {q['pergunta'][:100]}")
        for d in q["depois"][:5]:
            p(f"         {d['n']:>5}  {d['cliente']:<34} {str(d['proxima_nossa'])[:80]}")

    cm = R["caminhos"]
    p(f"\nCAMINHOS: saudação cliente {cm['saudacao_cliente']} · não-cliente {cm['saudacao_nao_cliente']['ocorrencias']}")
    for k, v in cm["ramos_depois_da_saudacao_de_cliente"].items():
        p(f"  {v['ocorrencias']:>5}  {v['descricao']}  msgs até o cliente falar: {v['env_ate_cliente_falar']}")

    p("\nINATIVIDADE")
    for sub in ("transferencia_por_inatividade", "sessao_expirou", "pesquisa_encerrada_inatividade"):
        v = R["inatividade"][sub]
        p(f"  {sub}: {v['total']} em {v['conversas']} conversas · tempo desde a anterior {v['tempo_desde_a_mensagem_anterior_s']}")
        p(f"     minutos mais comuns: {v['tempo_desde_a_mensagem_anterior_minutos_mais_comuns']}")
        p(f"     depois: {v['depois']}")
        for a in v["mensagem_anterior"][:6]:
            p(f"     {a['n']:>5}x logo após {a['anterior'][:100]}")
        for a in v["ultima_mensagem_nossa_antes"][:4]:
            p(f"     {a['n']:>5}x última nossa antes: {a['mensagem'][:90]}")

    p("\nPARES MAIS FREQUENTES (intervalo mediano)")
    for x in R["pares_top"][:TOP_N]:
        p(f"\n  {x['n']:>5}x  ({x['intervalo_mediano_s']}s)")
        p(f"        1. {x['a'][:120]}")
        p(f"        2. {x['b'][:120]}")

    d = R["duplicidades"]
    p("\nDUPLICIDADE — mesmo assunto na mesma rajada:", d["mesmo_assunto_na_mesma_rajada"])
    for x in d["mesmo_assunto_na_mesma_rajada_origens"][:12]:
        p(f"  {x['n']:>5}x {x['assunto']:<22} entre_fluxos={x['entre_fluxos']}  {x['primeiro'][:55]}  →  {x['repete_em'][:55]}")
    p("DUPLICIDADE — mesmo texto duas vezes seguidas:")
    for x in d["mesmo_texto_duas_vezes_seguidas"][:10]:
        p(f"  {x['n']:>5}x {x['texto'][:110]}")

    pr = R["proativas"]
    p(f"\nCONVERSAS QUE COMEÇAM COM MENSAGEM NOSSA: {pr['conversas_comecando_por_nos_na_amostra']} na amostra · "
      f"{pr['confirmadas_pela_api']} confirmadas ({pr['confirmadas_mas_iniciadas_antes_da_janela']} começaram antes da janela) · "
      f"{pr['cortadas_pela_janela']} cortadas pela janela · "
      f"{pr['nao_verificadas']} não verificadas")
    for x in pr["textos_da_primeira_rajada_top"][:12]:
        p(f"  {x['conversas']:>5} conv  {x['texto'][:110]}")

    p("\nCONVERSAS GIGANTES")
    for g in R["gigantes"]:
        p(f"  {g['id']}: {g['mensagens']} msgs ({g['enviadas']} env/{g['recebidas']} rec) · {g['dias']} dia(s) · "
          f"maior rajada {g['maior_rajada']} · fora janela {g['fora_janela_24h_true']} · "
          f"destinatários {g['destinatarios_distintos_por_campo']}")
        for t in g["mais_repetidas"][:2]:
            p(f"     {t['vezes']}x → {t.get('destinatarios_distintos')} destinatário(s), máx {t.get('max_vezes_para_um_mesmo_destinatario')} "
              f"p/ o mesmo, seguidas p/ o mesmo {t.get('repeticoes_seguidas_para_o_mesmo_destinatario')}: {t['texto'][:90]}")
    for n in R["notas"]:
        p("\nNOTA:", n)


# =============================================================================
# 12. Execução contra a API (dentro do pod)
# =============================================================================
def conectar():
    import django  # noqa: F401
    from apps.integrations.opa.atendimento import OpaAtendimentoSource
    from apps.integrations.shared.enums import Capability, SourceType
    from apps.shared.context import set_current_organization
    from apps.shared.decorators import allow_cross_tenant
    from apps.tenancy.models import Organization, OrganizationDataSource

    org = Organization.objects.get(slug=os.environ.get("OPA_V2_ORG", "velus"))
    set_current_organization(org)
    ds = allow_cross_tenant(reason="analise de rajadas v2")(
        lambda: OrganizationDataSource.objects.filter(
            organization=org,
            source_type=SourceType.OPA.value,
            capability=Capability.ATENDIMENTO.value,
            is_active=True,
        ).first()
    )()
    if ds is None:
        raise SystemExit("Fonte Opa/ATENDIMENTO ativa não encontrada para a organização.")
    creds = ds.get_credentials()
    return OpaAtendimentoSource(base_url=creds["base_url"], token=creds["token"])


def _get(client, filtro, limit, skip):
    r = client.get("atendimento/mensagem", json={"filter": filtro, "options": {"limit": limit, "skip": skip}})
    itens = r.get("data") if isinstance(r, dict) else None
    return itens if isinstance(itens, list) else []


def coletar(client):
    """Acha o fim da coleção por bissecção (a listagem global ignora filtro de
    data) e lê as PAGINAS*PAGE mensagens da ponta, em ordem de inserção."""
    teto = TETO_BISSECCAO
    for _ in range(6):
        if not _get(client, {}, 1, teto):
            break
        teto *= 2
    lo, hi = 0, teto
    while lo < hi:
        mid = (lo + hi) // 2
        if _get(client, {}, 1, mid):
            lo = mid + 1
        else:
            hi = mid
    total = lo
    alvo = PAGINAS * PAGE
    skip0 = skip = max(0, total - alvo)
    log(f"[v2] coleção com {total} mensagens; lendo a partir de {skip}")
    raws = []
    chamadas = 0
    while skip < total and chamadas < PAGINAS * 12:
        itens = _get(client, {}, PAGE, skip)
        chamadas += 1
        if not itens:
            break
        for k, raw in enumerate(itens):
            raw["_ordem"] = skip + k
            raws.append(raw)
        skip += len(itens)
        if chamadas % 5 == 0:
            log(f"[v2] {len(raws)} mensagens lidas")
    if skip < total:
        log(f"[aviso] parou em {skip} de {total} — a API pode estar limitando `limit` ({chamadas} chamadas)")
    return raws, {"total_na_colecao": total, "skip_inicial": skip0, "chamadas_de_pagina": chamadas,
                  "leu_ate_o_fim": skip >= total}


def main():
    src = conectar()
    catalogo = Catalogo(*carregar_nos())
    log(f"[v2] catálogo: {catalogo.resumo()}")
    with src._client_factory() as client:
        raws, meta = coletar(client)

        def buscar_rota(rota):
            return _get(client, {"id_rota": rota}, 1000, 0)

        R = analisar(raws, catalogo, buscar_rota=buscar_rota)
    R["janela"].update(meta)
    imprimir_resumo(R)
    print("\n===JSON_INICIO===")
    print(json.dumps(R, ensure_ascii=False, indent=1, default=_json_default))
    print("===JSON_FIM===")


def _json_default(o):
    if isinstance(o, set):
        return len(o)
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Counter):
        return dict(o)
    return str(o)


# =============================================================================
# 13. Autoteste (sem API) — textos sintéticos no formato dos pares do Anexo B
# =============================================================================
PIX_EXEMPLO = ("00020101021226850014br.gov.bcb.pix2563pixqrcode.sicredi.com.br/qr/v2/cobv/"
               "9d36b84fc70b478fb95c12729b90ca255204000053039865406119.905802BR5913VELUS INTERNET"
               "6008SOROCABA62070503***63041D3F")
CASOS = [
    # (mensagem, categoria esperada, subtipo/nó esperado contém, texto proibido na máscara)
    ({"titulo": 'Por favor, clique no botão "Ver Menu" e escolha uma opção de 1 a 5 para avaliar nosso atendimento, sendo 1 o menor valor 😞 e 5 o maior 😊. Caso queira encerrar a pesquisa, por favor digite /encerrar. \nObrigado!', "opcoes": [{"id": "5", "texto": "🤩 Excelente"}]},
     CAT_PESQ, "pesquisa_1a5", None),
    ("Sua sessão expirou!", CAT_INAT, "sessao_expirou", None),
    ("Seja bem vindo a Velus, seu protocolo para esse atendimento é: 2026091800123", CAT_FLUXO, "00 - Verifica se é cliente #4", "2026091800123"),
    ({"titulo": "Por favor, escolha uma das opções abaixo.", "opcoes": ["Contratar Internet", "Assistência Técnica"]}, CAT_FLUXO, "02 - Principal Entrada #1", None),
    ("Verifiquei aqui, que parece que você esta com algum problema de conexão, vou te transferir para o suporte.", CAT_FLUXO, "01 - Diagnóstico de Contrato #15", None),
    ("Muito obrigado por entrar em contato 😊, iniciaremos o atendimento o mais breve possível.", CAT_FLUXO, "Direto para Suporte #2", None),
    ("Ainda está ai? não se preocupe. estamos transferindo você para um de nossos atendentes.", CAT_INAT, "transferencia_por_inatividade", None),
    ("📢 AVISO IMPORTANTE 📢 Atendimento temporariamente encerrado. Retornaremos em breve.", CAT_AVISO, "temporariamente", None),
    (PIX_EXEMPLO, CAT_INTEG, "pix_codigo", "000201"),
    ({"titulo": "Posso te ajudar com mais alguma coisa?", "opcoes": ["Sim", "Não"]}, CAT_FLUXO, "Intermediário #1", None),
    ("Obrigado por escolher a Velus, estamos sempre trabalhando para entregar a melhor internet para nossos clientes. Para abrir um novo atendimento digite algo.", CAT_FLUXO, "Encerramento #1", None),
    ({"titulo": "Avalie nosso atendimento, de 0 a 5?"}, CAT_PESQ, "pesquisa_0a5", None),
    ("Fernanda alterou o departamento do atendimento para Financeiro", CAT_SIS, "troca_departamento", "Fernanda"),
    ("Segue código PIX do título com vencimento para 10/09/2026", CAT_INTEG, "pix_cabecalho", "10/09"),
    ("Olá! 👋 Me chamo Rodrigo e vou seguir com o seu atendimento", CAT_HUM, "apresentacao_atendente", "Rodrigo"),
    ("Poxa, infelizmente ainda não temos cobertura na sua região", CAT_HUM, "comercial_sem_cobertura", None),
    ("Opção inválida! Escolha uma das opções.", CAT_FLUXO, "(erro)", None),
    ("Escolha uma opção: 1 - Título com vencimento: 10/09/2026 R$ 99,90 2 - Título com vencimento: 10/10/2026 R$ 99,90", CAT_INTEG, "escolha_titulo", "99,90"),
    ("Agradecemos pelo contato! 😊 Foi um prazer atender você.", CAT_HUM, "despedida_atendente", None),
    ("Opção inválida!", CAT_INTEG, "opcao_invalida_integracao", None),
    ("Não consegui encontrar sua fatura, vou te passar para um atendente.", CAT_FLUXO, "01 - Diagnóstico de Contrato", None),
    ("Olá! 😊 O rompimento da fibra já foi resolvido e a conexão já foi normalizada.", CAT_AVISO, "rompimento", None),
    ("Desculpe não entendi, vou enviar para nosso menu principal", CAT_FLUXO, "01 - Diagnóstico de Contrato #1", None),
    ("Temos cobertura no seu endereço sim, abaixo seguem os nossos planos:", CAT_HUM, "comercial_planos", None),
    ("🔹 350 Mbps – R$ 79,90 🔹 750 Mbps – R$ 99,90* ⭐ Nosso plano mais vendido", CAT_HUM, "comercial_planos", "79,90"),
    ("Desculpe não consegui entender o que você gostaria, estou te passando para um humano, para te ajudar.", CAT_FLUXO, "02 - Principal Entrada #1 (limite)", None),
    ("Seu atendimento foi transferido para o um atendente humano, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 12:00 e das 13:30 às 18:00 horas e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.", CAT_FLUXO, "Direto para Triagem - Atendimento Humano #2", None),
    ("Olá MARIA DA SILVA SANTOS, seu protocolo para esse atendimento é: 2026091800456                                  📑 TEMOS UMA NOVIDADE para VOCÊ!    Conheça o App Velus 📱 Tudo em um só lugar: suas informações, suporte e PIX Recorrente, deixando seus pagamentos automáticos e sem preocupações. 📲 Baixe agora: • App Store: https://apps.apple.com/br/app/velus/id1569482649 • Play Store: https://play.google.com/store/apps/details?id=br.net.sorocabana.central Simples, rápido e na sua mão!",
     CAT_FLUXO, "00 - Verifica se é cliente #2", "MARIA"),
    ("Olá João Pereira, seu protocolo para esse atendimento é: 2026091800789 📑 TEMOS UMA NOVIDADE para VOCÊ!", CAT_FLUXO, "00 - Verifica se é cliente #2", "João"),
    ("Olá , seu protocolo para esse atendimento é: 2026091800790 📑 TEMOS UMA NOVIDADE para VOCÊ!", CAT_FLUXO, "00 - Verifica se é cliente #2", None),
    ("Consta em nosso sistema que há mensalidades em aberto no seu contrato. Regularize pelo PIX.", CAT_TPL, "cobranca_mensalidades", None),
    ("Seu contrato de Internet esta reduzido, você gostaria de realizar a liberação em confiança?", CAT_FLUXO, "Pergunta, Desbloqueio em Confiança #1", None),
    ({"titulo": "💬 Percebi que você está com um problema generalizado na sua região. Tem mais alguma coisa que posso te ajudar?"}, CAT_FLUXO, "Massiva #1", None),
    ("Seu atendimento foi transferido para o departamento de Comercial, nosso horário de atendimento é de segunda à sexta-feira das 08:00 às 22:00 horas e aos sábados das 08:00 às 12:00 horas, iniciaremos o atendimento o mais breve possível.", CAT_FLUXO, "Direto para o Comercial #1", None),
    ("Caso queira contratar nossos planos entre em https://assine.velusinternet.com.br ou aguarde para ser atendido", CAT_FLUXO, "Direto para o Comercial #1", "https"),
    ("*Carla Souza*:\nBoa tarde! Já verifiquei aqui o seu cadastro.", CAT_HUM, "assinatura_atendente", "Carla"),
    ("Meu CPF é 123.456.789-09, telefone (15) 99123-4567, email fulano@x.com", CAT_NC, None, "123.456"),
    ("Bom dia, Sr. João Pereira, tudo bem? Vou verificar.", CAT_NC, None, "João"),
    ("Prezado cliente JOÃO DA SILVA, identificamos uma pendência.", CAT_NC, None, "SILVA"),
    ("Olá, meu nome é Ana Paula e vou te ajudar hoje", CAT_HUM, "apresentacao_atendente", "Paula"),
    ("Carlos transferiu o atendimento para Fernanda Lima", CAT_SIS, "transferencia_manual", "Fernanda"),
    ("Fernanda alterou o departamento do atendimento para Suporte", CAT_SIS, "troca_departamento", "Fernanda"),
    ("Protocolo OPA2026091812345 aberto. Qualquer dúvida ligue 0800 319 9986 ou 15991234567.", CAT_NC, None, "12345"),
]


def autoteste():
    catalogo = Catalogo(*carregar_nos())
    print("catálogo:", catalogo.resumo())
    falhas = 0
    for msg, cat_esp, contem, proibido in CASOS:
        txt = texto_de(msg)[0]
        masc = mascarar(txt)
        ch = chave(masc)
        cat, sub, membros, fonte = classificar_texto(masc, ch, catalogo)
        no, _, _ = resolver_no(membros, None)
        alvo = f"{sub} {no or ''}"
        ok = cat == cat_esp and (contem is None or contem in alvo or contem in (masc or ""))
        if "Suporte" in txt and "alterou" in txt and "para Suporte" not in masc:
            ok = False  # departamento não pode ser mascarado
        if proibido and proibido in masc:
            ok = False
        falhas += 0 if ok else 1
        print(("OK   " if ok else "FALHA"), f"{cat:<20} {str(sub)[:28]:<28} {str(no)[:60]:<60} | {rotulo(masc, 90)}")
    # ponta a ponta com 3 conversas sintéticas
    base = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    def mk(rota, i, seg, direcao, msg, fj=None):
        d = {"_id": f"{rota}-{i}", "id_rota": rota, "mensagem": msg,
             "tipoDestinatario": "clientes_users" if direcao == "env" else "usuarios",
             "data": (base.replace(second=0) + __import__("datetime").timedelta(seconds=seg)).isoformat().replace("+00:00", "Z"),
             "tipo": "texto", "_ordem": i}
        if fj is not None:
            d["envioForaJanela24h"] = fj
        return d

    saud = CASOS[27][0]
    raws = []
    # c1: atraso → PIX → posso ajudar → inatividade → humano
    seq1 = [("rec", "oi"), ("env", saud), ("env", "Segue código PIX do título com vencimento para 10/09/2026"),
            ("env", PIX_EXEMPLO), ("env", {"titulo": "Posso te ajudar com mais alguma coisa?"}),
            ("env", "Ainda está ai? não se preocupe. estamos transferindo você"),
            ("env", "Olá! 👋 Me chamo Rodrigo e vou seguir com o seu atendimento")]
    segs1 = [0, 2, 3, 3, 4, 604, 900]
    # c2: sem conexão
    seq2 = [("rec", "sem internet"), ("env", CASOS[28][0]), ("env", CASOS[4][0]), ("env", CASOS[5][0]),
            ("env", "Olá! 👋 Me chamo Rodrigo e vou seguir com o seu atendimento"), ("rec", "ok")]
    segs2 = [0, 2, 3, 3, 400, 500]
    # c3: começa com mensagem nossa (cobrança 2x)
    cob = "Consta em nosso sistema que há mensalidades em aberto no seu contrato."
    seq3 = [("env", cob), ("env", cob)]
    segs3 = [0, 1]
    k = 0
    for rota, seq, segs in (("c1", seq1, segs1), ("c2", seq2, segs2), ("c3", seq3, segs3)):
        for (d, msg), s in zip(seq, segs):
            raws.append(mk(rota, k, s, d, msg, fj=(True if rota == "c3" and d == "env" else (False if d == "env" else None))))
            k += 1

    def fake_rota(r):
        return [x for x in raws if x["id_rota"] == r]

    R = analisar(raws, catalogo, buscar_rota=fake_rota)
    js = json.dumps(R, ensure_ascii=False, default=_json_default)
    for proib in ("MARIA", "Rodrigo", "2026091800", "000201", "Fernanda"):
        if proib in js:
            print("FALHA LGPD: apareceu", proib)
            falhas += 1
    ramos = R["caminhos"]["ramos_depois_da_saudacao_de_cliente"]
    esperado = {"financeiro_atraso": 1, "sem_conexao": 1}
    for k2, v in esperado.items():
        got = ramos.get(k2, {}).get("ocorrencias")
        print(("OK   " if got == v else "FALHA"), f"ramo {k2} = {got} (esperado {v})")
        falhas += 0 if got == v else 1
    got_inat = R["inatividade"]["transferencia_por_inatividade"]["total"]
    print(("OK   " if got_inat == 1 else "FALHA"), f"inatividade transferência = {got_inat}")
    falhas += 0 if got_inat == 1 else 1
    got_pro = R["proativas"]["confirmadas_pela_api"]
    print(("OK   " if got_pro == 1 else "FALHA"), f"proativas confirmadas = {got_pro}")
    falhas += 0 if got_pro == 1 else 1
    print("totais:", R["totais"]["enviadas"], "enviadas,", R["totais"]["rajadas"], "rajadas,", R["totais"]["excesso"], "excesso")
    print("tempo:", R["tempo_dentro_da_rajada"]["excesso_sincrono"], "síncrono,",
          R["tempo_dentro_da_rajada"]["excesso_assincrono"], "assíncrono")
    print("duplicidade assunto na rajada:", R["duplicidades"]["mesmo_assunto_na_mesma_rajada"])
    print(f"\nAUTOTESTE: {len(CASOS)} casos de texto + ponta a ponta — {falhas} falha(s)")
    return falhas


if os.environ.get("OPA_V2_SO_FUNCOES") != "1":
    if os.environ.get("OPA_V2_AUTOTESTE") == "1":
        autoteste()
    else:
        main()
