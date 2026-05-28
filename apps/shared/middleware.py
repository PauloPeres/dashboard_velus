"""Middleware que injeta a organização do user logado no contextvar."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .context import reset_current_organization, set_current_organization


class TenantMiddleware:
    """Lê `request.user.organization` e seta no contextvar pela duração da request.

    Requisitos:
    - User precisa ter atributo `.organization` (definido em apps.tenancy.User).
    - Esta middleware deve vir DEPOIS de AuthenticationMiddleware na lista.

    Comportamento:
    - User não autenticado → contexto fica None (TenantManager vai falhar em
      queries de domínio — esperado pra rotas que exigem auth).
    - User autenticado sem org → contexto fica None (idem; sócio sem membership
      ativo não deveria acessar dashboards).
    - User autenticado com org → contexto seta a org.

    O token de reset garante que o contextvar é restaurado mesmo se a view
    levantar exceção.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        org = None
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            # User custom em apps.tenancy expõe get_active_organization().
            # getattr defensivo pra suportar AnonymousUser e testes com User stub.
            get_org = getattr(user, "get_active_organization", None)
            if callable(get_org):
                org = get_org()

        token = set_current_organization(org)
        try:
            return self.get_response(request)
        finally:
            reset_current_organization(token)
