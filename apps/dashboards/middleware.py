"""Enforcement de acesso por página (RBAC por grupo, #65).

Roda depois de auth + tenant. Pra cada request que resolve numa view do
namespace `dashboards`, verifica se a membership ativa do usuário pode acessar
aquela aba (via grupo de acesso). OWNER vê tudo. Negado → redireciona pra
primeira aba permitida (ou pra página "sem acesso").

Escolha por middleware (em vez de decorar ~24 views): DRY e à prova de esquecer
de proteger uma view nova — toda rota do namespace passa por aqui.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse

from .pages import OWNER_ONLY, PAGE_KEYS, page_url, route_to_key


class PageAccessMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_view(
        self,
        request: HttpRequest,
        view_func: Callable[..., HttpResponse],  # noqa: ARG002
        view_args: tuple,  # noqa: ARG002
        view_kwargs: dict,  # noqa: ARG002
    ) -> HttpResponse | None:
        match = request.resolver_match
        if match is None:
            return None

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None  # login_required cuida

        membership = user.get_active_membership()
        if membership is None:
            return None  # sem membership → _require_org/login cuidam
        if membership.is_owner:
            return None  # owner vê tudo

        # Gestão de acesso (settings, grupos) é só do owner.
        if match.namespace == "dashboards" and match.url_name in OWNER_ONLY:
            return self._deny(membership)

        key = route_to_key(match.namespace, match.url_name)
        if key is None:
            return None  # rota fora do catálogo — não é aba protegida (ex.: home)

        allowed = membership.allowed_page_keys()
        if "*" in allowed or key in allowed:
            return None
        return self._deny(membership)

    @staticmethod
    def _deny(membership: object) -> HttpResponse:
        allowed = membership.allowed_page_keys()  # type: ignore[attr-defined]
        for k in PAGE_KEYS:
            if k in allowed:
                url = page_url(k)
                if url:
                    return redirect(reverse(url))
        return redirect(reverse("dashboards:no_access"))
