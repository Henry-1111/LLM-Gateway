import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.auth import Principal
from app.exceptions import GatewayError, StreamingNotSupportedError
from app.failover import should_failover
from app.metrics import (
    PROVIDER_ATTEMPTS,
    PROVIDER_DURATION,
    PROVIDER_FAILOVERS,
    TOKENS,
)
from app.repositories import ProviderAttemptRepository, RequestLogRepository
from app.routing import ProviderRouter
from app.schemas import ChatCompletionRequest


@dataclass(frozen=True)
class ChatResult:
    response: Dict[str, Any]
    request_id: str
    provider_name: str
    provider_attempts: int


class ChatService:
    def __init__(
        self,
        provider_router: ProviderRouter,
        request_logs: RequestLogRepository,
        provider_attempts: ProviderAttemptRepository,
        default_model: str,
        max_provider_attempts: int,
    ) -> None:
        self._provider_router = provider_router
        self._request_logs = request_logs
        self._provider_attempts = provider_attempts
        self._default_model = default_model
        self._max_provider_attempts = max_provider_attempts

    async def create_chat_completion(
        self,
        request: ChatCompletionRequest,
        principal: Principal,
        request_id: Optional[str] = None,
        provider_payload: Optional[Dict[str, Any]] = None,
    ) -> ChatResult:
        model = request.model or self._default_model
        request_id = request_id or str(uuid.uuid4())
        started_at = time.monotonic()
        await self._request_logs.start(request_id, principal, model)

        try:
            if request.stream:
                raise StreamingNotSupportedError()
            response, provider_name, attempts = await self._call_with_failover(
                request_id=request_id,
                model=model,
                payload=provider_payload
                if provider_payload is not None
                else request.provider_payload(self._default_model),
            )
        except GatewayError as exc:
            await self._request_logs.fail(
                request_id,
                exc.status_code,
                exc.code,
                self._elapsed_ms(started_at),
            )
            raise
        except Exception:
            await self._request_logs.fail(
                request_id,
                500,
                "internal_error",
                self._elapsed_ms(started_at),
            )
            raise

        await self._request_logs.succeed(
            request_id,
            response,
            self._elapsed_ms(started_at),
        )
        return ChatResult(
            response=response,
            request_id=request_id,
            provider_name=provider_name,
            provider_attempts=attempts,
        )

    async def _call_with_failover(
        self,
        request_id: str,
        model: str,
        payload: Dict[str, Any],
    ) -> tuple[Dict[str, Any], str, int]:
        candidates = self._provider_router.candidates(model)[: self._max_provider_attempts]

        for attempt_number, route in enumerate(candidates, start=1):
            await self._request_logs.set_provider(request_id, route.name)
            attempt_id = await self._provider_attempts.start(
                request_id=request_id,
                provider_name=route.name,
                attempt_number=attempt_number,
            )
            attempt_started_at = time.monotonic()
            try:
                response = await route.provider.create_chat_completion(payload)
            except GatewayError as exc:
                attempt_seconds = time.monotonic() - attempt_started_at
                PROVIDER_ATTEMPTS.labels(route.name, "failed", exc.code).inc()
                PROVIDER_DURATION.labels(route.name, "failed").observe(attempt_seconds)
                await self._provider_attempts.fail(
                    attempt_id,
                    exc.status_code,
                    exc.code,
                    self._elapsed_ms(attempt_started_at),
                )
                has_next_candidate = attempt_number < len(candidates)
                if should_failover(exc) and has_next_candidate:
                    next_route = candidates[attempt_number]
                    PROVIDER_FAILOVERS.labels(
                        route.name,
                        next_route.name,
                        exc.code,
                    ).inc()
                    continue
                raise
            except Exception:
                attempt_seconds = time.monotonic() - attempt_started_at
                PROVIDER_ATTEMPTS.labels(
                    route.name,
                    "failed",
                    "internal_error",
                ).inc()
                PROVIDER_DURATION.labels(route.name, "failed").observe(attempt_seconds)
                await self._provider_attempts.fail(
                    attempt_id,
                    500,
                    "internal_error",
                    self._elapsed_ms(attempt_started_at),
                )
                raise

            attempt_seconds = time.monotonic() - attempt_started_at
            PROVIDER_ATTEMPTS.labels(route.name, "succeeded", "none").inc()
            PROVIDER_DURATION.labels(route.name, "succeeded").observe(attempt_seconds)
            usage = response.get("usage") or {}
            for token_type, field_name in (
                ("prompt", "prompt_tokens"),
                ("completion", "completion_tokens"),
                ("total", "total_tokens"),
            ):
                token_value = usage.get(field_name)
                if isinstance(token_value, int) and token_value >= 0:
                    TOKENS.labels(route.name, token_type).inc(token_value)
            await self._provider_attempts.succeed(
                attempt_id,
                self._elapsed_ms(attempt_started_at),
            )
            return response, route.name, attempt_number

        raise RuntimeError("Provider candidates unexpectedly exhausted")

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return round((time.monotonic() - started_at) * 1000)
