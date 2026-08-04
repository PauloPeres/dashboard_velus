"""Linhagem de dados por página (#67) — de que fonte vêm os números de cada aba.

Mapa `page_key -> [ {source, detail} ]`, onde `source` é o rótulo do badge
(ex.: "Opa Suite", "IXC") e `detail` descreve as APIs/tabelas usadas (mostrado
no hover). Chave = mesma do catálogo de páginas (`pages.py`). Sem entrada → cai
no default (IXC).
"""

from __future__ import annotations

from .pages import route_to_key

_IXC = "IXC"
_OPA = "Opa Suite"
_INT = "Interno"

# Detalhes reutilizados.
_D_FIN = (
    "IXC (faturas, pagamentos, despesas, contratos) via sync → tabelas "
    "financial_invoice, financial_payment, financial_expense, customers_contract; "
    "agregações em analytics."
)
_D_CONTRACTS = "IXC (contratos/clientes) → customers_contract, customers_customer."
_D_OPA = (
    "Opa! Suite — APIs /atendimento, /etiqueta, /atendimento/motivo, "
    "/atendimento/mensagem → atendimento_atendimento, _etiqueta, _motivo, _mensagem."
)
_D_IXC_CUST = "IXC (clientes/contratos) → customers_customer, customers_contract."

LINEAGE: dict[str, list[dict[str, str]]] = {
    "executive": [{"source": _IXC, "detail": _D_FIN}],
    "revenue": [{"source": _IXC, "detail": _D_FIN}],
    "contracts": [{"source": _IXC, "detail": _D_CONTRACTS}],
    "financial": [{"source": _IXC, "detail": _D_FIN}],
    "cashflow": [{"source": _IXC, "detail": _D_FIN}],
    "forecast": [{"source": _IXC, "detail": _D_FIN}],
    "dre": [{"source": _IXC, "detail": _D_FIN}],
    "burn": [{"source": _IXC, "detail": _D_FIN}],
    "pessoas": [{"source": _IXC, "detail": "IXC (despesas de pessoal/prestadores) → financial_expense."}],
    "compromissos": [{"source": _IXC, "detail": "IXC (despesas OPEN futuras) → financial_expense."}],
    "descasamento": [{"source": _IXC, "detail": _D_FIN}],
    "churn": [
        {"source": _IXC, "detail": _D_IXC_CUST},
        {"source": _INT, "detail": "Modelo de churn (features + score) — analytics/ML interno."},
    ],
    "risk": [
        {"source": _IXC, "detail": _D_IXC_CUST},
        {"source": _INT, "detail": "Score de risco de churn — modelo interno."},
    ],
    "operations": [{"source": _IXC, "detail": "IXC (ordens de serviço/chamados) → helpdesk_ticket."}],
    "os_dashboard": [{"source": _IXC, "detail": "IXC (ordens de serviço) → helpdesk_ticket."}],
    "tecnicos": [{"source": _IXC, "detail": "IXC (OS por técnico) → helpdesk_ticket."}],
    "atendimento": [{"source": _OPA, "detail": _D_OPA}],
    "atendimento_tendencias": [
        {"source": _OPA, "detail": _D_OPA},
        {"source": _IXC, "detail": "IXC (faturas p/ marcador de vencimento) → financial_invoice."},
    ],
    "atendimento_conversao": [
        {"source": _OPA, "detail": _D_OPA},
        {"source": _IXC, "detail": "IXC (contratos: activated_at/canceled_at p/ conversão e churn) → customers_contract."},
    ],
    "conversas_ruins": [{"source": _OPA, "detail": _D_OPA}],
    "qa_supervisor": [
        {"source": _OPA, "detail": _D_OPA},
        {"source": _INT, "detail": "QA/LLM externo (Gemini) sobre mensagens redigidas."},
    ],
    "network": [{"source": _IXC, "detail": "IXC (rede/CTOs/banda) → network_* e snapshots."}],
    "sales": [{"source": _IXC, "detail": "IXC (leads/oportunidades) → sales_lead, sales_opportunity."}],
    "customers": [{"source": _IXC, "detail": _D_IXC_CUST}],
    "simuladores": [{"source": _INT, "detail": "Cenários/simulações — parâmetros internos, sem fonte externa."}],
    "sync": [{"source": _INT, "detail": "Estado das sincronizações — metadados internos (SyncJob/Checkpoint)."}],
}

_DEFAULT: list[dict[str, str]] = [{"source": _IXC, "detail": "Dados do ERP IXC via sincronização."}]

# Rotas que herdam a chave de acesso do pai (RBAC) mas têm fonte própria — a
# linhagem é da rota, não da aba. Ex.: a lista de uma hora (#76) lê só a Opa!,
# sem o marcador de vencimento (IXC) da página de tendências.
_ROUTE_LINEAGE: dict[tuple[str, str], list[dict[str, str]]] = {
    ("dashboards", "atendimento_hora"): [{"source": _OPA, "detail": _D_OPA}],
}


def lineage_for(namespace: str | None, url_name: str | None) -> list[dict[str, str]]:
    """Fontes de dados da página resolvida (badge + detalhe), ou [] se não é aba."""
    override = _ROUTE_LINEAGE.get((namespace or "", url_name or ""))
    if override is not None:
        return override
    key = route_to_key(namespace, url_name)
    if key is None:
        return []
    return LINEAGE.get(key, _DEFAULT)
