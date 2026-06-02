"""Classificação de assuntos de OS em categorias de atendimento.

Os nomes de assunto vêm do IXC (org-específicos, resolvidos via OsLookupCache).
Classificamos por palavras-chave no nome normalizado (minúsculo, sem acento).

Usado pelo motor de churn (#32): o sinal de "chamados frequentes" conta só
chamados que indicam insatisfação com o serviço (problema técnico/rede/suporte),
não rotinas como instalação, troca de equipamento, financeiro ou titularidade —
que antes inflavam o número de clientes em risco.

As regras são ordenadas: a primeira categoria cujas palavras-chave casam vence.
A ordem importa (ex.: "Agendar Instalação de Roteador Adicional" é INSTALL, não
EQUIPMENT/SUPPORT).
"""

from __future__ import annotations

import unicodedata

# ── Categorias ──────────────────────────────────────────────────────────
SUPPORT = "SUPPORT"        # problema técnico / rede / suporte — relevante p/ churn
LIFECYCLE = "LIFECYCLE"    # cancelamento / retenção / suspensão / desistência
INSTALL = "INSTALL"        # instalação / ativação / construção
EQUIPMENT = "EQUIPMENT"    # retirada / troca / setup box / app tv / cômodo
FINANCE = "FINANCE"        # boleto / cobrança / pagamento / desbloqueio
COMMERCIAL = "COMMERCIAL"  # venda / upgrade / mudança de plano
ADMIN = "ADMIN"            # titularidade / portabilidade / cadastro / vencimento
OTHER = "OTHER"            # não classificado

# Categorias que indicam insatisfação recorrente com o serviço — as únicas que
# contam pro sinal de "chamados frequentes" do churn.
CHURN_RELEVANT_CATEGORIES = frozenset({SUPPORT})

# Rótulos legíveis por categoria — usados nos cards de SLA por tipo (#34).
CATEGORY_LABELS: dict[str, str] = {
    SUPPORT: "Manutenção",
    INSTALL: "Instalação",
    EQUIPMENT: "Equipamento",
    FINANCE: "Financeiro",
    COMMERCIAL: "Comercial",
    LIFECYCLE: "Cancelamento/Retenção",
    ADMIN: "Administrativo",
    OTHER: "Outros",
}


def category_label(category: str) -> str:
    """Rótulo legível de uma categoria de atendimento (fallback = própria chave)."""
    return CATEGORY_LABELS.get(category, category)

# Regras (categoria, palavras-chave) avaliadas em ordem; primeira que casa vence.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        LIFECYCLE,
        (
            "cancelamento", "cancelar", "retenc", "desistiu", "cliente sumiu",
            "desativar contrato", "nao mora mais", "faleceu",
            "reprovado por conta consumo", "suspens",
        ),
    ),
    (
        INSTALL,
        (
            "instala", "instac", "reinstal", "ativacao de cliente",
            "construc", "viabilidade", "passagem de cabo",
        ),
    ),
    (
        EQUIPMENT,
        (
            "retirada", "retira", "troca de equip", "mudanca de equip",
            "setup box", "setupbox", "app tv", "roteador adicional",
            "mudanca de comodo", "mudanca local de ponto", "mudanca de aparelho",
            "novo controle", "hot spot", "desconexao",
        ),
    ),
    (
        FINANCE,
        (
            "boleto", "cobranc", "financeiro", "pagamento", "inadimpl",
            "renegoci", "desbloqueio", "liberac", "spc", "desconto",
            "comprovante", "debito", "fideliz", "vencimento",
        ),
    ),
    (
        COMMERCIAL,
        (
            "comercial", "venda", "alteracao de plano", "mudanca de plano",
            "mudanca de pacote", "upgrade", "pos venda",
        ),
    ),
    (
        ADMIN,
        (
            "titularidade", "portabilidade", "telefone", "linha de tel",
            "cadastro", "formulario", "conferencia", "auditar", "estoque",
            "compra material", "veiculo", "gerar todos os boletos", "numero",
        ),
    ),
    (
        SUPPORT,
        (
            "manutenc", "tubulac", "visita tecnica", "atendimento ao cliente",
            "acesso ao roteador", "queda em massa", "regulariz",
            "verificacao de rede", "verificacao n2", "instabil", "oscil",
            "lent", "sem sinal", "sem conex", "sem internet", "sem acesso",
            "reparo", "defeito", "intermit",
        ),
    ),
)


def _normalize(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name)
    no_accent = "".join(c for c in nfkd if not unicodedata.combining(c))
    return no_accent.casefold().strip()


def classify_subject(name: str | None) -> str:
    """Classifica o nome de um assunto de OS numa categoria de atendimento."""
    norm = _normalize(name or "")
    if not norm:
        return OTHER
    for category, keywords in _RULES:
        if any(kw in norm for kw in keywords):
            return category
    return OTHER


def churn_relevant_subject_ids(subject_map: dict[str, str]) -> set[str] | None:
    """IDs de assunto que contam pro sinal de chamados frequentes do churn.

    Retorna `None` quando o mapa está vazio (org sem lookups sincronizados),
    sinalizando ao chamador que deve usar o comportamento antigo (contar todos
    os chamados) — fallback gracioso pra não suprimir o sinal por falta de dados.
    """
    if not subject_map:
        return None
    return {
        sid
        for sid, name in subject_map.items()
        if classify_subject(name) in CHURN_RELEVANT_CATEGORIES
    }
