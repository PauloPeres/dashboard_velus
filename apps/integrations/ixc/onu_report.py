"""Parser do painel HTML do botão de potência da ONU (`botao_rel_22991`) — #148.

Este é o único ponto do projeto que lê **HTML de tela** em vez de JSON, e é por
isso que ele é paranoico. O endpoint não é uma API: é o relatório que o IXC
desenha para o operador, com marcação Bootstrap e entidades `iso-8859-1`. O
formato pode mudar no próximo release do ERP sem aviso, e quando mudar a
consequência autorizada é **ficar sem leitura** — nunca uma exceção subindo pelo
poll de status, que é o dado principal da aba de massivas.

Daí o contrato do módulo: `parse_onu_report_html` **não levanta nada**. Formato
irreconhecível, corpo vazio, página de erro, HTML truncado — tudo devolve um
dicionário vazio, que a camada de aplicação lê como "sem leitura".

Amostra real capturada em produção (2026-09-08, ONU 10733), reduzida ao corpo:

    <div>Causa da &#xFA;ltima queda: reset</div>
    <div>Sinal Rx: -18.57</div>
    <div>Temperatura: 40</div>
    <div>Voltagem: 3.320</div>
    <div>Sinal Tx: -23.19</div>
    <div>Status pot&#xEA;ncia: Regular</div>
    <div>--------------------------------------------------</div>
    <div>INFORMA&#xC7;&#xD5;ES ADICIONAIS:</div>
    <div>F/s/p: 0/4/3</div>
    <div>Run state: online</div>
    <div>Ont distance(m): 2369</div>
    <div>Mac: 485754435C5A78B9 (HWTC-5C5A78B9)</div>
    <div>Last up time: 08-09-2026 19:30:01-03:00</div>
    <div>Last dying gasp time: -</div>

Três coisas que a amostra real ensinou e que o parser depende:

1. **o painel traz a potência**, então não é preciso disparar e depois reler a
   listagem para obter o valor — a releitura fica de plano B, para o caso de o
   formato mudar;
2. **`-` é o sentinela de "a OLT não informou"** (`Last dying gasp time: -`), e
   aparece igual no `causa_ultima_queda` da listagem. Traço não é valor;
3. os rótulos vêm acentuados e como entidade HTML, logo o casamento é feito
   sobre o texto **sem acento e em minúsculas** — se o IXC trocar `&#xFA;` por
   `ú` literal, ou o `<div>` por `<td>`, o parser continua achando.
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

_logger = structlog.get_logger(__name__)

# Rótulo normalizado (sem acento, minúsculo) → chave neutra devolvida ao adapter.
# Mapa explícito, e não "tudo que parecer par rótulo:valor": o painel mistura
# medição com dados de gerência do equipamento (Mac router, Vendor sn, senhas em
# outros relatórios), e o dashboard só tem motivo para guardar o que mede a
# qualidade do enlace.
_LABELS: dict[str, str] = {
    "causa da ultima queda": "last_drop_cause",
    "sinal rx": "signal_rx",
    "sinal tx": "signal_tx",
    "temperatura": "temperature",
    "voltagem": "voltage",
    "status potencia": "power_status",
    "run state": "run_state",
    "control flag": "control_flag",
    "config state": "config_state",
    "last up time": "last_up_time",
    "last down time": "last_down_time",
    "last dying gasp time": "last_dying_gasp_time",
    "ont distance(m)": "ont_distance_m",
    "f/s/p": "fsp",
    "mac": "mac",
}

# Campos numéricos de potência/ambiente: zero é ausência de leitura, não valor.
# Mesma armadilha da listagem (ver `_to_optical_reading` em schemas.py).
_NUMERIC_FIELDS = frozenset({"signal_rx", "signal_tx", "temperature", "voltage"})

# Sentinelas de "a OLT não informou este campo".
_ABSENT_VALUES = frozenset({"", "-", "--", "n/a", "null"})

# `Last up time: 08-09-2026 19:30:01-03:00` — dia-mês-ano com offset explícito.
# A segunda forma existe porque o IXC não é consistente entre relatórios.
_DATETIME_FORMATS = ("%d-%m-%Y %H:%M:%S%z", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def parse_onu_report_html(body: str | bytes | None) -> dict[str, str]:
    """Extrai os pares rótulo→valor conhecidos do painel. **Nunca levanta.**

    Devolve dicionário de strings cruas (já sem os sentinelas de ausência). A
    conversão para número e data fica em `onu_report_fields`, para que quem só
    quiser ver o que a OLT disse possa ver o texto literal.
    """
    try:
        return _parse(body)
    except Exception:  # degradar é o requisito, não descuido — ver docstring
        # Painel de relatório: o formato pode mudar a qualquer release do ERP.
        # A ACL degrada para "sem leitura" e o poll de status segue vivo.
        _logger.warning("ixc_onu_report_parse_failed", exc_info=True)
        return {}


def onu_report_fields(body: str | bytes | None) -> dict[str, Any]:
    """Painel HTML → campos já tipados (float/datetime/str). **Nunca levanta.**

    Ausência continua ausência: campo que a OLT não informou simplesmente não
    aparece no dicionário — não vira zero, não vira string vazia.
    """
    parsed = parse_onu_report_html(body)
    if not parsed:
        return {}

    fields: dict[str, Any] = {}
    for key, raw in parsed.items():
        if key in _NUMERIC_FIELDS:
            value = _to_float_or_none(raw)
            if value is not None:
                fields[key] = value
        elif key.endswith("_time"):
            moment = _to_datetime_or_none(raw)
            if moment is not None:
                fields[key] = moment
        else:
            fields[key] = raw
    return fields


def _parse(body: str | bytes | None) -> dict[str, str]:
    if not body:
        return {}
    if isinstance(body, bytes):
        # O painel declara iso-8859-1 mas o conteúdo chega escapado em entidades
        # HTML; decodificar com tolerância é mais seguro que apostar no charset.
        body = body.decode("utf-8", errors="replace")

    # Tag vira quebra de linha: cada `<div>rótulo: valor</div>` sai numa linha,
    # e a mesma regra sobrevive se o IXC trocar div por td, li ou p.
    text = _html.unescape(_TAG_RE.sub("\n", body))

    found: dict[str, str] = {}
    for line in text.splitlines():
        label, _, value = line.partition(":")
        if not value:
            continue
        key = _LABELS.get(_normalize_label(label))
        if key is None or key in found:
            # Primeira ocorrência vence: o painel lista `Mac` do cliente antes de
            # `Mac router`, e o rótulo mais específico não deve sobrescrever.
            continue
        cleaned = _WS_RE.sub(" ", value).strip()
        if cleaned.lower() in _ABSENT_VALUES:
            continue
        found[key] = cleaned
    return found


def _normalize_label(label: str) -> str:
    """Minúsculo, sem acento e com espaços colapsados — casamento tolerante."""
    flat = unicodedata.normalize("NFKD", label)
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", flat).strip().lower()


def _to_float_or_none(raw: str) -> float | None:
    """`-18.57` → -18.57; `0.00` e lixo → None (zero é ausência de leitura)."""
    try:
        value = float(raw.replace(",", ".").split()[0])
    except (ValueError, IndexError):
        return None
    return None if value == 0.0 else value


def _to_datetime_or_none(raw: str) -> datetime | None:
    candidate = raw.split(" (")[0].strip()
    for fmt in _DATETIME_FORMATS:
        try:
            moment = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if moment.tzinfo is None:
            # Os formatos de fallback não trazem offset. O IXC opera no fuso do
            # provedor; datar em UTC aqui deslocaria o "voltou às" em 3 horas.
            return moment.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
        return moment
    return None
