"""Cliente HTTP da API Google Gemini (Generative Language) — generateContent.

Herda retry/throttle/classificação de erro do `BaseHttpAdapter`, evitando uma
dependência nova no cluster (não usamos o SDK oficial — mesma motivação da ML
pure-Python: deploy leve). Só expõe o necessário pra IA supervisora de QA:
`judge()` manda um system prompt + uma mensagem do usuário e devolve o texto.

Auth pelo header `x-goog-api-key` (mantém a chave fora da URL/logs). O modelo
entra no path: `/v1beta/models/{model}:generateContent`. A assinatura de
`judge()` é a mesma usada pelo `qa_review` — que é agnóstico de provedor.
Ver: https://ai.google.dev/api/generate-content
"""

from __future__ import annotations

from typing import Any, ClassVar

from apps.integrations.shared.http_client import BaseHttpAdapter

GEMINI_API_BASE = "https://generativelanguage.googleapis.com"


class GeminiClient(BaseHttpAdapter):
    """Cliente mínimo da API generateContent do Gemini.

    Uso como context manager (herdado):

        with GeminiClient(api_key=...) as client:
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
        base_url: str = GEMINI_API_BASE,
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
        headers["x-goog-api-key"] = self._api_key
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
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        response = self.post(f"/v1beta/models/{model}:generateContent", json=payload)
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Concatena o texto das `parts` do primeiro candidato da resposta."""
        if not isinstance(response, dict):
            return ""
        parts_text: list[str] = []
        for candidate in response.get("candidates") or []:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            if not isinstance(content, dict):
                continue
            for part in content.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts_text.append(part["text"])
        return "".join(parts_text).strip()
