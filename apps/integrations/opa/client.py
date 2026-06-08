"""Cliente HTTP do Opa! Suite (atendimento/WhatsApp omnichannel).

Peculiaridades da API Opa!:
- Auth Bearer (JWT) no header `Authorization`.
- Endpoints de listagem usam GET com body JSON: `{"filter": {...}, "options":
  {"limit": N, "skip": M}}`. `limit` maximo 100.
- **Paginacao = `options.skip`** (NAO `page`/`offset`, que sao ignorados).
- Response wrapper: `{"status", "code", "data": [...]}` (lista) ou
  `{"data": {...}}` (objeto populado).
- Datas em ISO 8601 (Mongo) com timezone.

Escopo read-only: apenas GETs. Nenhum POST/PUT/PATCH/DELETE e exposto aqui —
preserva a natureza analitica/read-only do Velus.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

from apps.integrations.shared.http_client import BaseHttpAdapter


class OpaHttpClient(BaseHttpAdapter):
    """Cliente Opa! Suite — herda retry/throttle do BaseHttpAdapter, sobrescreve
    paginacao pro formato Opa! (GET com body + `options.skip`)."""

    default_rate_limit_per_second: ClassVar[int] = 4
    default_page_size: ClassVar[int] = 100

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float | None = None,
        rate_limit_per_second: int | None = None,
    ) -> None:
        # base_url nas credenciais e o dominio (https://opasuite.xxx.net.br/);
        # a API vive em /api/v1/.
        api_base = base_url.rstrip("/") + "/api/v1/"
        self._token = str(token)
        super().__init__(
            base_url=api_base,
            timeout=timeout,
            rate_limit_per_second=rate_limit_per_second,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = super()._build_headers()
        headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # -------------------------------------------------------------------------
    # Paginacao Opa! — `options.skip` (page/offset sao ignorados pela API)
    # -------------------------------------------------------------------------
    def paginate_opa(
        self,
        path: str,
        *,
        filter: dict[str, Any] | None = None,
        page_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Itera registros de um endpoint de listagem via skip.

        Cada chamada faz GET com body `{"filter": ..., "options": {limit, skip}}`.
        Yield item-a-item ate a pagina vir com menos que `limit`.
        """
        size = page_size or self.default_page_size
        skip = 0
        base_filter = dict(filter or {})

        while True:
            body = {
                "filter": base_filter,
                "options": {"limit": size, "skip": skip},
            }
            response = self.get(path, json=body)
            items = self._extract_list(response)
            if not items:
                return
            yield from items
            if len(items) < size:
                return
            skip += len(items)

    def get_one(self, path: str) -> dict[str, Any] | None:
        """GET de um objeto populado (ex.: `atendimento/{id}`). Retorna o `data`."""
        response = self.get(path)
        if not isinstance(response, dict):
            return None
        data = response.get("data", response)
        return data if isinstance(data, dict) else None

    @staticmethod
    def _extract_list(response: Any) -> list[dict[str, Any]]:
        if not isinstance(response, dict):
            return []
        data = response.get("data", [])
        return data if isinstance(data, list) else []
