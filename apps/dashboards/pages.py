"""Catálogo central de páginas ("abas") — fonte única pro nav, o RBAC por grupo
(#65) e a linhagem de dados (#67).

"Fonte única pro nav" passou a ser verdade: o menu lateral era uma segunda
lista, escrita à mão em `base.html`, e a página de Mensagens & Canais nasceu
invisível justamente por isso — registrada aqui, ausente lá. Agora `nav_tree()`
monta a árvore do menu a partir DESTA lista, e o template só desenha.

Cada página declara:

- `key`      — identificador estável, usado nos grupos de acesso (não renomear);
- `label`    — nome na UI de grupos de acesso;
- `url`      — rota namespaced (reverse);
- `section`  — agrupamento na tela de grupos de acesso;
- `nav_label`   (opcional) — texto no menu, quando difere do `label`. Existe
  porque o menu tem espaço pra "Fluxo de Caixa" e a tela de permissões prefere
  o curto "Caixa";
- `group`       (opcional) — submenu recolhível do menu lateral ("Despesas");
- `active_on`   (opcional) — url_names extras que acendem o item (as rotas de
  detalhe, que não têm entrada própria);
- `extra_links` (opcional) — links irmãos que compartilham a MESMA chave de
  acesso (o DRE tem dois: gerencial e por conta).

Rotas de detalhe/sub herdam a chave do pai via `_ROUTE_TO_KEY`.
"""

from __future__ import annotations

from typing import Any

from django.urls import NoReverseMatch, reverse

# Ordem = ordem do menu.
PAGES: list[dict[str, Any]] = [
    {"key": "executive", "label": "Executivo", "url": "dashboards:executive", "section": "Estratégico"},
    {"key": "revenue", "label": "Receita", "url": "dashboards:revenue", "section": "Financeiro"},
    {"key": "contracts", "label": "Contratos", "url": "dashboards:contracts", "section": "Financeiro"},
    {"key": "financial", "label": "Inadimplência", "url": "dashboards:financial", "section": "Financeiro"},
    {"key": "cashflow", "label": "Caixa", "nav_label": "Fluxo de Caixa", "url": "dashboards:cashflow", "section": "Financeiro", "group": "Despesas"},
    {"key": "dre", "label": "DRE", "nav_label": "DRE Gerencial", "url": "dashboards:dre", "section": "Financeiro", "group": "Despesas",
     "extra_links": [{"nav_label": "Fluxo de Caixa por Conta", "url": "dashboards:dre_detalhe"}]},
    {"key": "burn", "label": "Burn rate", "nav_label": "Burn Rate", "url": "dashboards:burn", "section": "Financeiro", "group": "Despesas"},
    {"key": "forecast", "label": "Forecast", "nav_label": "Previsão 12m", "url": "dashboards:forecast", "section": "Financeiro", "group": "Despesas"},
    {"key": "pessoas", "label": "Pessoas", "nav_label": "Pessoas & Prestadores", "url": "dashboards:pessoas", "section": "Financeiro", "group": "Despesas"},
    {"key": "compromissos", "label": "Compromissos", "nav_label": "Compromissos Futuros", "url": "dashboards:compromissos", "section": "Financeiro", "group": "Despesas"},
    {"key": "descasamento", "label": "Descasamento", "nav_label": "Descasamento de Caixa", "url": "dashboards:descasamento", "section": "Financeiro", "group": "Despesas"},
    {"key": "churn", "label": "Churn", "url": "dashboards:churn", "section": "Risco"},
    {"key": "risk", "label": "Risco de Churn", "url": "dashboards:risk", "section": "Risco"},
    {"key": "operations", "label": "Operações", "url": "dashboards:operations", "section": "Operações"},
    {"key": "os_dashboard", "label": "Ordens de Serviço", "url": "dashboards:os_dashboard", "section": "Operações"},
    {"key": "tecnicos", "label": "Técnicos", "url": "dashboards:tecnicos", "section": "Operações"},
    {"key": "atendimento", "label": "Atendimento", "url": "dashboards:atendimento", "section": "Operações"},
    {"key": "atendimento_tendencias", "label": "Tendências de Atendimento", "url": "dashboards:atendimento_tendencias", "section": "Operações"},
    {"key": "mensagens", "label": "Mensagens & Canais", "url": "dashboards:mensagens", "section": "Operações"},
    {"key": "atendimento_conversao", "label": "Conversão & Churn", "url": "dashboards:atendimento_conversao", "section": "Operações"},
    {"key": "conversas_ruins", "label": "Conversas Ruins", "url": "dashboards:conversas_ruins", "section": "Operações", "active_on": ["atendimento_detail"]},
    {"key": "qa_supervisor", "label": "QA de Atendimento", "url": "dashboards:qa_supervisor", "section": "Operações"},
    {"key": "massivas", "label": "Quedas & Massivas", "url": "dashboards:massivas", "section": "Operações", "active_on": ["massiva_detalhe"]},
    {"key": "network", "label": "Rede", "url": "dashboards:network", "section": "Operações"},
    {"key": "sales", "label": "Vendas / CRM", "url": "dashboards:sales", "section": "Comercial"},
    {"key": "customers", "label": "Clientes 360", "url": "dashboards:customers", "section": "Comercial", "active_on": ["customer_detail"]},
    {"key": "simuladores", "label": "Simuladores", "url": "scenarios:pj_vs_clt", "section": "Ferramentas"},
    {"key": "sync", "label": "Sync", "url": "sync:status", "section": "Ferramentas"},
]

PAGE_KEYS: list[str] = [p["key"] for p in PAGES]
_BY_KEY: dict[str, dict[str, Any]] = {p["key"]: p for p in PAGES}

# (namespace, url_name) -> key. Inclui aliases de rotas de detalhe/sub.
_ROUTE_TO_KEY: dict[tuple[str, str], str] = {}
for _p in PAGES:
    _ns, _name = _p["url"].split(":", 1)
    _ROUTE_TO_KEY[(_ns, _name)] = _p["key"]
_ROUTE_TO_KEY.update({
    ("dashboards", "dre_detalhe"): "dre",
    ("dashboards", "customer_detail"): "customers",
    ("dashboards", "atendimento_detail"): "conversas_ruins",
    ("dashboards", "atendimento_hora"): "atendimento_tendencias",
    # Lista genérica de atendimentos (#87) — mesma chave da tela da hora, que
    # ela absorveu. Ver `_ROUTE_EXTRA_KEYS`: o acesso a ela não é exclusivo dessa
    # aba.
    ("dashboards", "atendimento_lista"): "atendimento_tendencias",
    # Detalhe de uma massiva e o partial de auto-refresh herdam a aba (#146).
    ("dashboards", "massiva_detalhe"): "massivas",
    ("dashboards", "massivas_abertas"): "massivas",
    # CRUD de eventos de rede (#78) mora na página de Tendências.
    ("dashboards", "evento_rede_novo"): "atendimento_tendencias",
    ("dashboards", "evento_rede_editar"): "atendimento_tendencias",
})

# Rotas que mais de uma aba pode abrir: basta ter QUALQUER uma das chaves.
#
# Existe por causa da lista genérica de atendimentos (#87/#89). Ela é o destino
# dos drill-downs de DUAS abas — Tendências e Atendimento —, e a chave única
# barrava quem só tinha "Atendimento": clicar numa barra de "Volume por
# departamento" caía em negação de acesso.
#
# Descartadas as outras saídas:
# - aba própria pra lista: ela não é uma aba (não entra no nav, não é um recorte
#   de dados diferente) e obrigaria a mexer em todo grupo de acesso já criado;
# - derivar a chave do `?origem=` da querystring: o parâmetro é do usuário, então
#   qualquer um driblaria o RBAC trocando `origem=tendencias` por
#   `origem=atendimento`. Autorização não pode sair de input do cliente.
#
# Conceder às duas abas é seguro porque a lista não mostra nada além do que as
# duas telas de origem já mostram: os mesmos atendimentos da mesma org, só que
# linha a linha.
_ROUTE_EXTRA_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ("dashboards", "atendimento_lista"): ("atendimento",),
    ("dashboards", "atendimento_hora"): ("atendimento",),
}


# Rotas do namespace dashboards restritas ao OWNER (gestão de acesso).
OWNER_ONLY: set[str] = {"settings", "access_management"}


def route_to_key(namespace: str | None, url_name: str | None) -> str | None:
    """Chave de acesso da rota (namespace:url_name), ou None se não protegida."""
    if not url_name:
        return None
    return _ROUTE_TO_KEY.get((namespace or "", url_name))


def route_access_keys(namespace: str | None, url_name: str | None) -> tuple[str, ...]:
    """Chaves que dão acesso à rota — ter QUALQUER uma basta (#89).

    Separado de `route_to_key` de propósito: a linhagem de dados (#67) e o nav
    querem a aba *canônica* da rota (uma só), enquanto o enforcement quer todas
    as que autorizam. Vazio = rota fora do catálogo (não protegida).
    """
    key = route_to_key(namespace, url_name)
    if key is None:
        return ()
    return (key, *_ROUTE_EXTRA_KEYS.get((namespace or "", url_name or ""), ()))


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


def _nav_label(page: dict[str, Any]) -> str:
    return page.get("nav_label") or page["label"]


def _reverse_or_none(url_name: str) -> str | None:
    """Resolve a rota; None se ela não existir.

    Uma página do catálogo sem rota registrada é erro de código, mas derrubar o
    menu inteiro (e com ele todas as páginas) por causa de uma linha errada é
    pior que omitir o item — o teste de catálogo é quem tem que pegar isso.
    """
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return None


def _pode_ver(page: dict[str, Any], allowed_all: bool, allowed_pages: set[str]) -> bool:
    return allowed_all or page["key"] in allowed_pages


def _monta_item(
    page: dict[str, Any], url_name_atual: str | None
) -> dict[str, Any] | None:
    """Um item de menu (com os links irmãos que dividem a mesma chave)."""
    href = _reverse_or_none(page["url"])
    if href is None:
        return None

    proprio = page["url"].split(":", 1)[1]
    acende = {proprio, *page.get("active_on", [])}

    links = [
        {
            "label": _nav_label(page),
            "href": href,
            "ativo": url_name_atual == proprio,
        }
    ]
    for extra in page.get("extra_links", []):
        extra_href = _reverse_or_none(extra["url"])
        if extra_href is None:
            continue
        extra_name = extra["url"].split(":", 1)[1]
        acende.add(extra_name)
        links.append(
            {
                "label": extra["nav_label"],
                "href": extra_href,
                "ativo": url_name_atual == extra_name,
            }
        )

    return {
        "key": page["key"],
        "links": links,
        # `ativo` do item cobre as rotas de detalhe também: estando em
        # `customer_detail`, "Clientes 360" fica aceso.
        "ativo": url_name_atual in acende,
    }


def nav_tree(
    *,
    allowed_all: bool,
    allowed_pages: set[str],
    url_name_atual: str | None,
) -> list[dict[str, Any]]:
    """Árvore do menu lateral, já filtrada pelo que o usuário pode ver.

    Devolve uma lista de nós na ordem de `PAGES`, cada um `{"tipo": ...}`:

    - `"link"`  — item simples, com um ou mais `links`;
    - `"grupo"` — submenu recolhível (`label`, `slug`, `aberto`, `itens`).

    Um grupo só aparece se sobrar algum item visível dentro dele, e nasce aberto
    quando a página atual está lá dentro. O template não decide nada disso: ele
    percorre a árvore e desenha.
    """
    arvore: list[dict[str, Any]] = []
    grupos_por_nome: dict[str, dict[str, Any]] = {}

    for page in PAGES:
        if not _pode_ver(page, allowed_all, allowed_pages):
            continue
        item = _monta_item(page, url_name_atual)
        if item is None:
            continue

        nome_grupo = page.get("group")
        if nome_grupo is None:
            arvore.append({"tipo": "link", **item})
            continue

        grupo = grupos_por_nome.get(nome_grupo)
        if grupo is None:
            grupo = {
                "tipo": "grupo",
                "label": nome_grupo,
                "slug": nome_grupo.lower().replace(" ", "-"),
                "aberto": False,
                "itens": [],
            }
            grupos_por_nome[nome_grupo] = grupo
            # O grupo entra na posição do seu PRIMEIRO item — é o que mantém a
            # ordem do menu igual à ordem declarada em PAGES.
            arvore.append(grupo)
        grupo["itens"].append(item)
        if item["ativo"]:
            grupo["aberto"] = True

    return arvore
