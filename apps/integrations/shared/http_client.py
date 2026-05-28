"""Base HTTP client para adapters externos.

Centraliza:
- Retry exponencial com `tenacity` (só pra transient)
- Rate limit cliente-side (token bucket simples)
- Classificação de erros HTTP em hierarquia tipada de exceções
- Logging estruturado com contexto

Adapters concretos (IxcHttpClient, SgpHttpClient, ...) herdam, sobrescrevem
`_build_headers()` se precisarem, e implementam métodos de domínio
(`list_customers`, etc.) chamando `self.get()`/`self.post()`/`self.paginate()`.

Uso como context manager pra garantir cleanup do httpx.Client:

    with IxcHttpClient(base_url=..., user_id=..., api_token=...) as client:
        for customer in client.paginate("/clientes"):
            ...
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from types import TracebackType
from typing import Any, ClassVar, Self

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .exceptions import (
    AdapterAuthError,
    AdapterClientError,
    AdapterError,
    AdapterTransientError,
)

_logger = structlog.get_logger(__name__)

# Headers default — sobrescrevíveis por subclasse via `_build_headers()`.
_DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Velus-Dashboard/0.1 (+https://velus.com.br)",
}


class BaseHttpAdapter:
    """Cliente HTTP base — herdado por adapters concretos (IXC, SGP, etc.).

    Atributos de classe (sobrescritíveis):
    - `default_timeout`: timeout das requests em segundos.
    - `default_rate_limit_per_second`: limite client-side conservador. Adapters
      podem aumentar conforme rate limit real do sistema externo for descoberto.
    - `retry_max_attempts`: nº máximo de tentativas em transient errors.
    - `retry_max_wait_seconds`: cap superior do backoff exponencial.
    """

    default_timeout: ClassVar[float] = 30.0
    default_rate_limit_per_second: ClassVar[int] = 5
    retry_max_attempts: ClassVar[int] = 5
    retry_max_wait_seconds: ClassVar[float] = 30.0

    def __init__(
        self,
        base_url: str,
        *,
        auth: httpx.Auth | tuple[str, str] | None = None,
        timeout: float | None = None,
        rate_limit_per_second: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout if timeout is not None else self.default_timeout
        self.rate_limit_per_second = (
            rate_limit_per_second
            if rate_limit_per_second is not None
            else self.default_rate_limit_per_second
        )
        self._client: httpx.Client | None = None
        self._last_request_at: float = 0.0

    # -------------------------------------------------------------------------
    # Context manager — abre/fecha httpx.Client
    # -------------------------------------------------------------------------
    def __enter__(self) -> Self:
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=self.auth,
            timeout=self.timeout,
            headers=self._build_headers(),
            follow_redirects=True,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _build_headers(self) -> dict[str, str]:
        """Sobrescreva em subclasse pra adicionar headers específicos (X-API-Key, etc.)."""
        return dict(_DEFAULT_HEADERS)

    # -------------------------------------------------------------------------
    # Throttle simples (token bucket de profundidade 1)
    # -------------------------------------------------------------------------
    def _throttle(self) -> None:
        if self.rate_limit_per_second <= 0:
            return
        min_interval = 1.0 / self.rate_limit_per_second
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_at = time.monotonic()

    # -------------------------------------------------------------------------
    # Request com retry (apenas em transient errors)
    # -------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Faz request, classifica resposta, retry exponencial em transient."""
        if self._client is None:
            raise RuntimeError(
                f"{type(self).__name__} deve ser usado como context manager: "
                f"`with adapter as client: ...`"
            )

        # tenacity precisa ser aplicado ao método interno (não decorator dinâmico no self).
        retry_decorator = retry(
            retry=retry_if_exception_type(AdapterTransientError),
            stop=stop_after_attempt(self.retry_max_attempts),
            wait=wait_exponential(
                multiplier=1, min=1, max=self.retry_max_wait_seconds
            ),
            reraise=True,
        )
        return retry_decorator(self._do_request)(method, path, params=params, json=json)

    def _do_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
    ) -> Any:
        assert self._client is not None  # garantido em `request`
        self._throttle()
        log = _logger.bind(method=method, path=path)

        try:
            response = self._client.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            log.warning("http_timeout", error=str(exc))
            raise AdapterTransientError(f"Timeout em {method} {path}") from exc
        except httpx.NetworkError as exc:
            log.warning("http_network_error", error=str(exc))
            raise AdapterTransientError(f"Network error em {method} {path}") from exc

        status = response.status_code

        if status in (401, 403):
            log.error("http_auth_error", status=status)
            raise AdapterAuthError(
                f"Autenticação falhou em {method} {path}: HTTP {status}"
            )
        if status == 429:
            retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
            log.warning("http_rate_limit", retry_after_seconds=retry_after)
            time.sleep(retry_after)
            raise AdapterTransientError(f"Rate limit em {method} {path}")
        if 500 <= status < 600:
            log.warning("http_server_error", status=status)
            raise AdapterTransientError(
                f"Servidor erro {status} em {method} {path}"
            )
        if 400 <= status < 500:
            log.error("http_client_error", status=status, body=response.text[:200])
            raise AdapterClientError(
                f"HTTP {status} em {method} {path}: {response.text[:200]}"
            )

        log.debug("http_request_ok", status=status)
        try:
            return response.json()
        except ValueError as exc:
            raise AdapterError(
                f"Resposta não-JSON em {method} {path}: {response.text[:200]}"
            ) from exc

    @staticmethod
    def _parse_retry_after(header_value: str | None) -> float:
        if not header_value:
            return 5.0
        try:
            return max(1.0, float(header_value))
        except ValueError:
            return 5.0

    # -------------------------------------------------------------------------
    # Conveniências
    # -------------------------------------------------------------------------
    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    # -------------------------------------------------------------------------
    # Paginação padrão — offset-based. Sobrescreva em adapter se sistema usar
    # cursor (next_token) ou page-based (page=N).
    # -------------------------------------------------------------------------
    def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = 100,
        items_key: str = "data",
    ) -> Iterator[dict[str, Any]]:
        """Itera offset por offset, yield item-a-item.

        Pressupõe resposta com chave `data` (ou `items_key` configurável) contendo
        a lista da página. Adapter sobrescreve se sistema tem formato diferente.
        """
        offset = 0
        base_params = dict(params or {})
        while True:
            paged_params = {**base_params, "limit": page_size, "offset": offset}
            response = self.get(path, params=paged_params)
            items = response.get(items_key, []) if isinstance(response, dict) else []
            if not items:
                return
            yield from items
            if len(items) < page_size:
                return
            offset += len(items)
