"""Context processors dos dashboards."""
from __future__ import annotations

from typing import Any

from django.http import HttpRequest

from .period import period_context as _period_context


def period_context(request: HttpRequest) -> dict[str, Any]:
    """Expõe o filtro de período da página, quando ela resolveu um (#86).

    O período é resolvido na view (`period.get_period`), que marca o request;
    aqui só traduzimos pra variáveis de template. Página sem período resolvido
    devolve dict vazio — e o partial da barra simplesmente não renderiza.
    """
    return _period_context(request)


def page_access(request: HttpRequest) -> dict[str, Any]:
    """Expõe ao template as abas que o usuário pode ver (RBAC por grupo, #65).

    `allowed_all` = True pro OWNER (ou quando não há membership resolvida — evita
    esconder tudo por engano). `allowed_pages` = conjunto de keys liberadas.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"allowed_all": False, "allowed_pages": set(), "is_owner": False}
    membership = user.get_active_membership()
    if membership is None:
        # Sem membership o middleware não bloqueia; não escondemos o nav.
        return {"allowed_all": True, "allowed_pages": set(), "is_owner": False}
    allowed = membership.allowed_page_keys()
    return {
        "allowed_all": "*" in allowed,
        "allowed_pages": allowed,
        "is_owner": membership.is_owner,
    }


def data_lineage(request: HttpRequest) -> dict[str, Any]:
    """Expõe as fontes de dados da página atual (badge + hover, #67)."""
    from .data_lineage import lineage_for

    match = getattr(request, "resolver_match", None)
    if match is None:
        return {"data_sources": []}
    return {"data_sources": lineage_for(match.namespace, match.url_name)}
