"""Cliente HTTP do IXC Soft.

Peculiaridades da API IXC:
- Endpoints de listagem usam método GET com body JSON (atípico, mas é como funciona).
- Header `ixcsoft: listar` diferencia listagem de outras operações sobre o mesmo recurso.
- Auth Basic com `Base64(user_id:api_token)`.
- Paginação é page-based: `page` + `rp` (registros por página).
- Response wrapper: `{"page": "1", "total": "1234", "registros": [...]}`.
- Datas em string formato `YYYY-MM-DD HH:MM:SS` (sem timezone).

Documentação oficial varia entre versões do ERP — quando schema mudar, falha
cedo na validação Pydantic (Anti-Corruption Layer no IxcCustomerSource).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

import httpx

from apps.integrations.shared.http_client import BaseHttpAdapter


class IxcHttpClient(BaseHttpAdapter):
    """Cliente IXC — herda retry/throttle/auth do BaseHttpAdapter, sobrescreve
    paginação pro formato IXC (page-based + body em GET)."""

    # Conservador inicial — descobrir empiricamente em bootstrap.
    default_rate_limit_per_second: ClassVar[int] = 3
    default_page_size: ClassVar[int] = 100

    def __init__(
        self,
        *,
        base_url: str,
        user_id: str,
        api_token: str,
        timeout: float | None = None,
        rate_limit_per_second: int | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            auth=httpx.BasicAuth(username=str(user_id), password=str(api_token)),
            timeout=timeout,
            rate_limit_per_second=rate_limit_per_second,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = super()._build_headers()
        # `ixcsoft: listar` é necessário em endpoints de listagem
        # (em outras operações, não setamos — adapter decide por método).
        return headers

    # -------------------------------------------------------------------------
    # Paginação IXC — formato proprietário
    # -------------------------------------------------------------------------
    def paginate_ixc(
        self,
        resource: str,
        *,
        body_filter: dict[str, Any] | None = None,
        page_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Itera todos os registros de um recurso (cliente, contrato, etc.).

        Cada chamada faz GET no recurso com body JSON contendo filtros + page.
        Yield item-a-item até que a página retorne menos que `rp`.

        `body_filter` opcional pra incluir filtros como `data_alteracao >= since`.
        """
        if self._client is None:
            raise RuntimeError(
                f"{type(self).__name__} deve ser usado em context manager."
            )

        size = page_size or self.default_page_size
        page = 1

        while True:
            body: dict[str, Any] = dict(body_filter or {})
            body["page"] = str(page)
            body["rp"] = str(size)

            # IXC: GET com body é peculiar — httpx aceita content em GET.
            # Adicionamos header ixcsoft: listar.
            self._throttle()
            response = self._client.request(
                "GET",
                resource,
                content=__import__("json").dumps(body).encode("utf-8"),
                headers={"ixcsoft": "listar"},
            )
            # Reusa classificação do base (erros HTTP) via método auxiliar
            payload = self._classify_and_extract(response, resource)

            registros = payload.get("registros") or []
            if not registros:
                return
            yield from registros

            total_str = payload.get("total") or "0"
            try:
                total = int(total_str)
            except ValueError:
                total = 0

            if page * size >= total or len(registros) < size:
                return
            page += 1

    def _classify_and_extract(self, response: httpx.Response, path: str) -> dict[str, Any]:
        """Replica a classificação de erros do BaseHttpAdapter pro request inline.

        Necessário porque sobrescrevemos o request (com content) e não passamos
        pelo `self.request()` que já faz isso. Pode-se refatorar pra reuso,
        mas mantenho explícito enquanto o adapter IXC for o único atípico.
        """
        from apps.integrations.shared.exceptions import (
            AdapterAuthError,
            AdapterClientError,
            AdapterError,
            AdapterTransientError,
        )

        status = response.status_code
        if status in (401, 403):
            raise AdapterAuthError(f"Auth falhou em IXC {path}: HTTP {status}")
        if status == 429:
            raise AdapterTransientError(f"Rate limit IXC em {path}")
        if 500 <= status < 600:
            raise AdapterTransientError(f"IXC erro {status} em {path}")
        if 400 <= status < 500:
            raise AdapterClientError(
                f"IXC HTTP {status} em {path}: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(
                f"IXC resposta não-JSON em {path}: {response.text[:200]}"
            ) from exc
        if not isinstance(data, dict):
            raise AdapterError(f"IXC resposta inesperada em {path}: {type(data).__name__}")
        return data
