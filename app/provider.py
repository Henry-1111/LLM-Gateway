from typing import Any, Dict

import httpx

from app.exceptions import (
    UpstreamConnectionError,
    UpstreamResponseError,
    UpstreamTimeoutError,
)


class OpenAICompatibleProvider:
    """Calls a provider that implements the OpenAI chat-completions contract."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        base_url: str,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def create_chat_completion(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError() from exc
        except httpx.RequestError as exc:
            raise UpstreamConnectionError() from exc

        if response.is_error:
            raise UpstreamResponseError(
                status_code=response.status_code,
                detail=self._safe_error_message(response),
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamResponseError(
                status_code=response.status_code,
                detail="The model provider returned invalid JSON.",
            ) from exc

        if not isinstance(data, dict) or "choices" not in data:
            raise UpstreamResponseError(
                status_code=response.status_code,
                detail="The model provider returned an invalid response.",
            )
        return data

    @staticmethod
    def _safe_error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return "The model provider returned an error."
        error = body.get("error", {}) if isinstance(body, dict) else {}
        message = error.get("message") if isinstance(error, dict) else None
        return message if isinstance(message, str) else "The model provider returned an error."
