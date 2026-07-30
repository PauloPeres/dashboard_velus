"""Catálogo central de páginas ("abas") — fonte única pro nav, o RBAC por grupo
(#65) e a futura linhagem de dados (#67).

Cada página: `key` estável (usada nos grupos de acesso), `label`, `url` (nome
namespaced pra reverse/nav) e `section` (agrupamento). Rotas de detalhe/sub
herdam a chave do pai via `ROUTE_ALIASES`.
"""

from __future__ import annotations

from typing import Any

# Ordem = ordem do menu.
PAGES: list[dict[str, str]] = [
    {"key": "executive", "label": "Executivo", "url": "dashboards:executive", "section": "Estratégico"},
    {"key": "revenue", "label": "Receita", "url": "dashboards:revenue", "section": "Financeiro"},
    {"key": "contracts", "label": "Contratos", "url": "dashboards:contracts", "section": "Financeiro"},
    {"key": "financial", "label": "Inadimplência", "url": "dashboards:financial", "section": "Financeiro"},
    {"key": "cashflow", "label": "Caixa", "url": "dashboards:cashflow", "section": "Financeiro"},
    {"key": "forecast", "label": "Forecast", "url": "dashboards:forecast", "section": "Financeiro"},
    {"key": "dre", "label": "DRE", "url": "dashboards:dre", "section": "Financeiro"},
    {"key": "burn", "label": "Burn rate", "url": "dashboards:burn", "section": "Financeiro"},
    {"key": "pessoas", "label": "Pessoas", "url": "dashboards:pessoas", "section": "Financeiro"},
    {"key": "compromissos", "label": "Compromissos", "url": "dashboards:compromissos", "section": "Financeiro"},
    {"key": "descasamento", "label": "Descasamento", "url": "dashboards:descasamento", "section": "Financeiro"},
    {"key": "churn", "label": "Churn", "url": "dashboards:churn", "section": "Risco"},
    {"key": "risk", "label": "Risco de Churn", "url": "dashboards:risk", "section": "Risco"},
    {"key": "operations", "label": "Operações", "url": "dashboards:operations", "section": "Operações"},
    {"key": "os_dashboard", "label": "Ordens de Serviço", "url": "dashboards:os_dashboard", "section": "Operações"},
    {"key": "tecnicos", "label": "Técnicos", "url": "dashboards:tecnicos", "section": "Operações"},
    {"key": "atendimento", "label": "Atendimento", "url": "dashboards:atendimento", "section": "Operações"},
    {"key": "atendimento_tendencias", "label": "Tendências de Atendimento", "url": "dashboards:atendimento_tendencias", "section": "Operações"},
    {"key": "atendimento_conversao", "label": "Conversão & Churn", "url": "dashboards:atendimento_conversao", "section": "Operações"},
    {"key": "conversas_ruins", "label": "Conversas Ruins", "url": "dashboards:conversas_ruins", "section": "Operações"},
    {"key": "qa_supervisor", "label": "QA de Atendimento", "url": "dashboards:qa_supervisor", "section": "Operações"},
    {"key": "network", "label": "Rede", "url": "dashboards:network", "section": "Operações"},
    {"key": "sales", "label": "Vendas / CRM", "url": "dashboards:sales", "section": "Comercial"},
    {"key": "customers", "label": "Clientes 360", "url": "dashboards:customers", "section": "Comercial"},
    {"key": "simuladores", "label": "Simuladores", "url": "scenarios:pj_vs_clt", "section": "Ferramentas"},
    {"key": "sync", "label": "Sync", "url": "sync:status", "section": "Ferramentas"},
]

PAGE_KEYS: list[str] = [p["key"] for p in PAGES]
_BY_KEY: dict[str, dict[str, str]] = {p["key"]: p for p in PAGES}

# (namespace, url_name) -> key. Inclui aliases de rotas de detalhe/sub.
_ROUTE_TO_KEY: dict[tuple[str, str], str] = {}
for _p in PAGES:
    _ns, _name = _p["url"].split(":", 1)
    _ROUTE_TO_KEY[(_ns, _name)] = _p["key"]
_ROUTE_TO_KEY.update({
    ("dashboards", "dre_detalhe"): "dre",
    ("dashboards", "customer_detail"): "customers",
    ("dashboards", "atendimento_detail"): "conversas_ruins",
})


# Rotas do namespace dashboards restritas ao OWNER (gestão de acesso).
OWNER_ONLY: set[str] = {"settings", "access_management"}


def route_to_key(namespace: str | None, url_name: str | None) -> str | None:
    """Chave de acesso da rota (namespace:url_name), ou None se não protegida."""
    if not url_name:
        return None
    return _ROUTE_TO_KEY.get((namespace or "", url_name))


def page_url(key: str) -> str | None:
    p = _BY_KEY.get(key)
    return p["url"] if p else None


def sections() -> list[dict[str, Any]]:
    """Páginas agrupadas por seção, preservando a ordem — pra UI de grupos."""
    out: list[dict[str, Any]] = []
    for p in PAGES:
        if not out or out[-1]["section"] != p["section"]:
            out.append({"section": p["section"], "pages": []})
        out[-1]["pages"].append(p)
    return out
