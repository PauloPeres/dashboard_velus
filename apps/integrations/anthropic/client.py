"""Cliente HTTP da API Anthropic (Claude) — Messages API.

Herda retry/throttle/classificação de erro do `BaseHttpAdapter`, evitando uma
dependência nova no cluster (não usamos o SDK oficial — mesma motivação da ML
pure-Python: deploy leve). Só expõe o necessário pra IA supervisora de QA:
`judge()` manda um system prompt + uma mensagem do usuário e devolve o texto.

Auth por header `x-api-key` (não Bearer). A versão da API vai no header
`anthropic-version`. Ver: https://docs.anthropic.com/en/api/messages
"""

from __future__ import annotations

from typing import Any, ClassVar

from apps.integrations.shared.http_client import BaseHttpAdapter

ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicClient(BaseHttpAdapter):
    """Cliente mínimo da Messages API do Claude.

    Uso como context manager (herdado):

        with AnthropicClient(api_key=...) as client:
            texto = client.judge(system=rubrica, user=transcricao, model="...")
    """

    # A API aguenta bem mais; conservador pra não estourar rate limit em lote.
    default_rate_limit_per_second: ClassVar[int] = 2
    # Geração é mais lenta que um GET de ERP — timeout generoso.
    default_timeout: ClassVar[float] = 60.0

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = ANTHROPIC_API_BASE,
        timeout: float | None = None,
        rate_limit_per_second: int | None = None,
    ) -> None:
        self._api_key = api_key
        super().__init__(
            base_url=base_url,
            timeout=timeout,
            rate_limit_per_second=rate_limit_per_second,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = super()._build_headers()
        headers["x-api-key"] = self._api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
        return headers

    def judge(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        """Manda 1 turno (system + user) e devolve o texto concatenado da resposta.

        `temperature=0.0` por default — avaliação de QA quer consistência, não
        criatividade. Levanta `AdapterError`/`AdapterAuthError`/... em falha.
        """
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        response = self.post("/v1/messages", json=payload)
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Concatena os blocos `type=text` do campo `content` da resposta."""
        if not isinstance(response, dict):
            return ""
        blocks = response.get("content") or []
        parts = [
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts).strip()
