"""Filtro de período dos dashboards — componente reutilizável (#86).

Nasceu dentro de `views.py` pra a página de Tendências de Atendimento (#75) e
virou módulo próprio quando passou a ser o filtro padrão do dashboard inteiro,
no lugar do `<select>` de meses da sidebar.

API pública:

- `get_period(request, ...)` — resolve a janela da página **uma vez** por
  request, já deixando no `request` o que o context processor e o middleware
  precisam (contexto de template e cookie de persistência).
- `set_period_extra_params(request, params)` — parâmetros da página que o form
  de período personalizado precisa propagar (`foco`, `g`, `departamento`, …).
- `period_context(request)` — usado pelo context processor homônimo; devolve as
  variáveis de template do badge e da barra de presets.

Precedência de resolução (a **URL sempre vence**):

1. `?de=&ate=` (personalizado)
2. `?periodo=` (preset)
3. URLs antigas `?months=N` → `Nm` e `?hd=N` → `Nd`
4. cookie `velus_periodo` (valor inválido é ignorado **em silêncio** — o usuário
   não digitou aquilo, então não cabe `warning`)
5. default da página (30 dias na granularidade em dias, 12 meses na mensal)

Toda resolução de data acontece em America/Sao_Paulo.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.http import HttpRequest
from django.utils import timezone

TZ = ZoneInfo("America/Sao_Paulo")

MAX_MONTHS = 24
MAX_DAYS = 366 * 2  # teto de 24 meses, também pro formato em dias

COOKIE_NAME = "velus_periodo"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90  # 90 dias

# Chave própria pro "Ontem": ele não cabe no formato `Nd`, que é sempre ancorado
# em hoje. Ver o docstring de `_period_ontem`.
KEY_ONTEM = "ontem"

# Presets exibidos como botões na granularidade em dias. Outros valores no
# formato Nd/Nm continuam aceitos (URLs antigas ?months=/?hd= caem aqui).
DAY_PRESETS: tuple[tuple[str, str], ...] = (
    ("1d", "Hoje"),
    (KEY_ONTEM, "Ontem"),
    ("7d", "Últimos 7 dias"),
    ("14d", "Últimos 14 dias"),
    ("30d", "Últimos 30 dias"),
    ("3m", "Últimos 3 meses"),
    ("6m", "Últimos 6 meses"),
    ("12m", "Últimos 12 meses"),
)

# Granularidade mensal: páginas de série mensal (DRE, MRR, burn, forecast…) só
# sabem trabalhar com N meses inteiros. Os presets em dias não aparecem lá.
MONTH_PRESETS: tuple[tuple[str, str], ...] = (
    ("1m", "Mês atual"),
    ("2m", "2 meses"),
    ("3m", "3 meses"),
    ("6m", "6 meses"),
    ("12m", "12 meses"),
    ("24m", "24 meses"),
)

DEFAULT_DAY_KEY = "30d"
DEFAULT_MONTH_KEY = "12m"

_PRESET_RE = re.compile(r"^(\d{1,3})([dm])$")
_CUSTOM_COOKIE_RE = re.compile(r"^custom:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")

_PT_MONTHS = ("", "Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
              "Jul", "Ago", "Set", "Out", "Nov", "Dez")

# Nomes de todos os presets conhecidos (pro rótulo não depender da lista exibida).
_PRESET_NAMES = dict(DAY_PRESETS)


@dataclass(frozen=True)
class Period:
    """Janela de análise resolvida uma única vez por request.

    `start`/`end` são datetimes aware (America/Sao_Paulo), ambos inclusivos:
    `start` é o primeiro instante do dia inicial e `end` é o último instante do
    dia final (ou "agora", quando o dia final é hoje).
    """

    key: str  # preset ("7d", "3m", "ontem", …) ou "custom"
    start: datetime
    end: datetime
    label: str
    warning: str | None = None

    @property
    def is_custom(self) -> bool:
        return self.key == "custom"

    @property
    def start_date(self) -> date:
        return self.start.date()

    @property
    def end_date(self) -> date:
        return self.end.date()

    @property
    def query(self) -> str:
        """Fragmento de querystring que reproduz o período (propagação #75)."""
        if self.is_custom:
            return f"de={self.start_date.isoformat()}&ate={self.end_date.isoformat()}"
        return f"periodo={self.key}"

    @property
    def cookie_value(self) -> str:
        """Valor persistido em `velus_periodo` (lido de volta por `_from_cookie`)."""
        if self.is_custom:
            return f"custom:{self.start_date.isoformat()}:{self.end_date.isoformat()}"
        return self.key

    @property
    def months(self) -> int:
        """Quantos meses inteiros a janela cobre (páginas de série mensal)."""
        m = _PRESET_RE.match(self.key)
        if m and m.group(2) == "m":
            return int(m.group(1))
        days = (self.end_date - self.start_date).days + 1
        return max(1, min(MAX_MONTHS, math.ceil(days / 30)))


# =============================================================================
# Construção de janelas
# =============================================================================
def _today() -> date:
    return timezone.localtime(timezone.now(), TZ).date()


def _fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _month_label(d: date) -> str:
    return f"{_PT_MONTHS[d.month]}/{d.year}"


def _bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Datas inclusivas → instantes aware; o fim nunca passa de agora."""
    now = timezone.localtime(timezone.now(), TZ)
    start = datetime.combine(start_date, time.min, tzinfo=TZ)
    end = datetime.combine(end_date, time.max, tzinfo=TZ)
    return start, min(end, now)


def _period_preset(n: int, unit: str) -> Period:
    """Preset de N dias/meses ancorado em hoje (ambos os extremos inclusivos)."""
    today = _today()
    if unit == "d":
        start_date = today - timedelta(days=n - 1)
        default_name = f"Últimos {n} dias"
    else:
        start_date = today - relativedelta(months=n) + timedelta(days=1)
        default_name = f"Últimos {n} meses"
    key = f"{n}{unit}"
    name = _PRESET_NAMES.get(key, default_name)
    start, end = _bounds(start_date, today)
    label = f"{name} ({_fmt_date(start_date)} – {_fmt_date(today)})"
    return Period(key=key, start=start, end=end, label=label)


def _period_ontem() -> Period:
    """"Ontem": chave própria `periodo=ontem`, resolvida aqui.

    Decisão (#86): "ontem" não cabe no formato `Nd`, que é sempre ancorado em
    hoje. As duas saídas possíveis eram emitir `de=&ate=` da véspera ou criar
    uma chave. Ficou a **chave própria**: assim o preset continua *relativo*
    como todos os outros (um link compartilhado com `?periodo=ontem` mostra a
    véspera de quem abrir, não uma data congelada), o badge segue dizendo
    "Ontem" em vez de virar "Personalizado", e o botão continua marcado como
    ativo quando a página recarrega.
    """
    d = _today() - timedelta(days=1)
    start, end = _bounds(d, d)
    return Period(key=KEY_ONTEM, start=start, end=end, label=f"Ontem ({_fmt_date(d)})")


def _month_period(n: int) -> Period:
    """Janela de N meses-calendário terminando no mês corrente (série mensal).

    As agregações mensais (`compute_*(org, months=N)`) contam N buckets de mês
    fechando no mês atual — o rótulo aqui reflete exatamente isso.
    """
    n = max(1, min(MAX_MONTHS, n))
    today = _today()
    first_bucket = (today - relativedelta(months=n - 1)).replace(day=1)
    start, end = _bounds(first_bucket, today)
    name = dict(MONTH_PRESETS).get(f"{n}m", f"{n} meses")
    if n == 1:
        label = f"{name} ({_month_label(today)})"
    else:
        label = f"{name} ({_month_label(first_bucket)} – {_month_label(today)})"
    return Period(key=f"{n}m", start=start, end=end, label=label)


def _from_key(key: str) -> Period | None:
    """Constrói o período de uma chave de preset. None quando inválida."""
    if key == KEY_ONTEM:
        return _period_ontem()
    m = _PRESET_RE.match(key)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n < 1:
        return None
    if (unit == "d" and n > MAX_DAYS) or (unit == "m" and n > MAX_MONTHS):
        return None
    return _period_preset(n, unit)


def _parse_custom(raw_de: str, raw_ate: str, default_key: str) -> Period:
    """Valida `?de=&ate=` (YYYY-MM-DD, inclusivos). Inválido → default + aviso."""
    def fallback(msg: str) -> Period:
        base = _from_key(default_key) or _period_preset(30, "d")
        return replace(base, warning=msg)

    try:
        start_date = date.fromisoformat(raw_de)
        end_date = date.fromisoformat(raw_ate)
    except ValueError:
        return fallback("Datas inválidas — mostrando os últimos 30 dias.")

    today = _today()
    if end_date < start_date:
        return fallback("Data final antes da inicial — mostrando os últimos 30 dias.")
    if end_date > today:
        return fallback("Data final no futuro — mostrando os últimos 30 dias.")
    if start_date < end_date - relativedelta(months=MAX_MONTHS):
        return fallback(
            f"Período maior que {MAX_MONTHS} meses — mostrando os últimos 30 dias."
        )

    start, end = _bounds(start_date, end_date)
    label = f"Período: {_fmt_date(start_date)} – {_fmt_date(end_date)}"
    return Period(key="custom", start=start, end=end, label=label)


def _from_cookie(raw: str) -> Period | None:
    """Lê o cookie. Qualquer valor estranho devolve None (ignorado em silêncio)."""
    raw = raw.strip()
    if not raw:
        return None
    m = _CUSTOM_COOKIE_RE.match(raw)
    if m:
        period = _parse_custom(m.group(1), m.group(2), DEFAULT_DAY_KEY)
        return None if period.warning else period
    return _from_key(raw)


# =============================================================================
# Resolução por request
# =============================================================================
def _resolve_from_url(request: HttpRequest, default_key: str) -> Period | None:
    """Período pedido explicitamente na URL (inclui as formas legadas)."""
    raw_de = request.GET.get("de", "").strip()
    raw_ate = request.GET.get("ate", "").strip()
    if raw_de or raw_ate:
        return _parse_custom(raw_de, raw_ate, default_key)

    raw = request.GET.get("periodo", "").strip()
    if not raw:
        # URLs antigas. Quando as duas vêm juntas, `months` vence: ela governava
        # a maior parte das páginas.
        legacy_months = request.GET.get("months", "").strip()
        legacy_days = request.GET.get("hd", "").strip()
        if legacy_months.isdigit():
            raw = f"{legacy_months}m"
        elif legacy_days.isdigit():
            raw = f"{legacy_days}d"
    if not raw:
        return None
    return _from_key(raw) or (_from_key(default_key) or _period_preset(30, "d"))


def _to_month_granularity(period: Period, *, from_url: bool) -> Period:
    """Ajusta um período qualquer pra a granularidade mensal.

    Página de série mensal só sabe pedir `months=N` às agregações. Períodos em
    dias (Hoje, Ontem, 7d…) e personalizados não têm tradução honesta ali —
    caem no default de 12 meses. Se o pedido veio da URL, avisa; se veio do
    cookie (o usuário escolheu "Ontem" em outra aba), fica quieto.
    """
    m = _PRESET_RE.match(period.key)
    if m and m.group(2) == "m":
        return replace(_month_period(int(m.group(1))), warning=period.warning)
    fallback = _month_period(int(DEFAULT_MONTH_KEY[:-1]))
    if not from_url:
        return fallback
    return replace(
        fallback,
        warning=(
            "Esta página trabalha em meses fechados — mostrando os últimos "
            f"{fallback.months} meses."
        ),
    )


def get_period(
    request: HttpRequest,
    *,
    granularity: str = "day",
    presets: tuple[tuple[str, str], ...] | None = None,
    default_key: str | None = None,
    allow_custom: bool | None = None,
    show_label: bool = False,
    show_bar: bool = True,
) -> Period:
    """Resolve o período da página e prepara contexto de template + cookie.

    `granularity="month"` é pra páginas de série mensal: só presets em meses,
    sem período personalizado, default de 12 meses.

    `show_bar=False` resolve o período sem desenhar a barra — pra página que já
    tem controle temporal próprio (DRE detalhado) e só usa o valor como
    fallback.
    """
    is_month = granularity == "month"
    if presets is None:
        presets = MONTH_PRESETS if is_month else DAY_PRESETS
    if default_key is None:
        default_key = DEFAULT_MONTH_KEY if is_month else DEFAULT_DAY_KEY
    if allow_custom is None:
        allow_custom = not is_month

    period = _resolve_from_url(request, default_key)
    from_url = period is not None
    if period is None:
        period = _from_cookie(request.COOKIES.get(COOKIE_NAME, ""))
    if period is None:
        period = _from_key(default_key) or _period_preset(30, "d")

    if is_month:
        period = _to_month_granularity(period, from_url=from_url)

    # Só a URL grava o cookie: navegar não deve sobrescrever a escolha por causa
    # do default de uma página.
    if from_url:
        request.velus_period_cookie = period.cookie_value  # type: ignore[attr-defined]

    request.velus_period = period  # type: ignore[attr-defined]
    request.velus_period_presets = presets  # type: ignore[attr-defined]
    request.velus_period_allow_custom = allow_custom  # type: ignore[attr-defined]
    request.velus_period_show_label = show_label  # type: ignore[attr-defined]
    request.velus_period_show_bar = show_bar  # type: ignore[attr-defined]
    return period


def set_period_extra_params(request: HttpRequest, params: dict[str, Any]) -> None:
    """Params da página que o form de período personalizado precisa preservar.

    Evita hidden input hardcoded por template: a view diz o que é seu recorte
    (`g`, `foco`, `departamento`, `motivo`, `tag`, …) e o partial propaga.
    Valores vazios/None são descartados.
    """
    request.velus_period_extra_params = {  # type: ignore[attr-defined]
        k: v for k, v in params.items() if v not in (None, "")
    }


def period_context(request: HttpRequest) -> dict[str, Any]:
    """Variáveis de template do filtro. Vazio quando a página não tem período."""
    period: Period | None = getattr(request, "velus_period", None)
    if period is None:
        return {}
    presets = getattr(request, "velus_period_presets", DAY_PRESETS)
    return {
        "period": period,
        "period_label": period.label,
        "period_warning": period.warning,
        "period_query": period.query,
        "period_presets": [
            {"key": key, "name": name, "active": period.key == key}
            for key, name in presets
        ],
        "period_de": period.start_date.isoformat(),
        "period_ate": period.end_date.isoformat(),
        "period_extra_params": getattr(request, "velus_period_extra_params", {}),
        "period_allow_custom": getattr(request, "velus_period_allow_custom", True),
        "period_show_label": getattr(request, "velus_period_show_label", False),
        "period_show_bar": getattr(request, "velus_period_show_bar", True),
    }


def apply_period_cookie(request: HttpRequest, response: Any) -> None:
    """Persiste a escolha vinda da URL (chamado pelo middleware)."""
    value = getattr(request, "velus_period_cookie", None)
    if not value:
        return
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=COOKIE_MAX_AGE,
        httponly=True,  # só o servidor lê
        samesite="Lax",
        secure=getattr(settings, "SESSION_COOKIE_SECURE", False),
    )
