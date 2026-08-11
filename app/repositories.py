import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import Principal
from app.events import request_event_payload
from app.models import OutboxEvent, ProviderAttempt, RequestLog


logger = logging.getLogger("llm_gateway.requests")


class RequestLogRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def start(
        self,
        request_id: str,
        principal: Principal,
        model: str,
    ) -> None:
        async with self._sessions() as session:
            session.add(
                RequestLog(
                    id=request_id,
                    tenant_id=principal.tenant_id,
                    api_key_id=principal.api_key_id,
                    model=model,
                    status="started",
                )
            )
            await session.commit()

    async def succeed(
        self,
        request_id: str,
        response: Dict[str, Any],
        duration_ms: int,
    ) -> None:
        usage = response.get("usage") or {}
        await self._complete(
            request_id=request_id,
            status="succeeded",
            status_code=200,
            duration_ms=duration_ms,
            prompt_tokens=self._token_value(usage.get("prompt_tokens")),
            completion_tokens=self._token_value(usage.get("completion_tokens")),
            total_tokens=self._token_value(usage.get("total_tokens")),
        )

    async def set_provider(self, request_id: str, provider_name: str) -> None:
        async with self._sessions() as session:
            request_log = await session.get(RequestLog, request_id)
            if request_log is None:
                raise RuntimeError(f"Request log {request_id} does not exist")
            request_log.provider_name = provider_name
            await session.commit()

    async def fail(
        self,
        request_id: str,
        status_code: int,
        error_code: str,
        duration_ms: int,
    ) -> None:
        await self._complete(
            request_id=request_id,
            status="failed",
            status_code=status_code,
            duration_ms=duration_ms,
            error_code=error_code,
        )

    async def _complete(
        self,
        request_id: str,
        status: str,
        status_code: int,
        duration_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        error_code: Optional[str] = None,
    ) -> None:
        async with self._sessions() as session:
            request_log = await session.get(RequestLog, request_id)
            if request_log is None:
                raise RuntimeError(f"Request log {request_id} does not exist")
            request_log.status = status
            request_log.status_code = status_code
            request_log.prompt_tokens = prompt_tokens
            request_log.completion_tokens = completion_tokens
            request_log.total_tokens = total_tokens
            request_log.duration_ms = duration_ms
            request_log.error_code = error_code
            request_log.completed_at = datetime.now(timezone.utc)
            event_id = str(uuid.uuid4())
            event_type = (
                "request.completed" if status == "succeeded" else "request.failed"
            )
            session.add(
                OutboxEvent(
                    id=event_id,
                    event_type=event_type,
                    aggregate_id=request_id,
                    payload=json.dumps(
                        request_event_payload(request_log, event_id, event_type),
                        separators=(",", ":"),
                    ),
                    status="pending",
                )
            )
            await session.commit()

        logger.info(
            json.dumps(
                {
                    "request_id": request_id,
                    "status": status,
                    "status_code": status_code,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "duration_ms": duration_ms,
                    "error_code": error_code,
                },
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _token_value(value: Any) -> int:
        return value if isinstance(value, int) and value >= 0 else 0


class ProviderAttemptRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions

    async def start(
        self,
        request_id: str,
        provider_name: str,
        attempt_number: int,
    ) -> str:
        attempt = ProviderAttempt(
            request_id=request_id,
            provider_name=provider_name,
            attempt_number=attempt_number,
            status="started",
        )
        async with self._sessions() as session:
            session.add(attempt)
            await session.commit()
            await session.refresh(attempt)
        return attempt.id

    async def succeed(self, attempt_id: str, duration_ms: int) -> None:
        await self._complete(
            attempt_id=attempt_id,
            status="succeeded",
            status_code=200,
            duration_ms=duration_ms,
        )

    async def fail(
        self,
        attempt_id: str,
        status_code: int,
        error_code: str,
        duration_ms: int,
    ) -> None:
        await self._complete(
            attempt_id=attempt_id,
            status="failed",
            status_code=status_code,
            error_code=error_code,
            duration_ms=duration_ms,
        )

    async def _complete(
        self,
        attempt_id: str,
        status: str,
        status_code: int,
        duration_ms: int,
        error_code: Optional[str] = None,
    ) -> None:
        async with self._sessions() as session:
            attempt = await session.get(ProviderAttempt, attempt_id)
            if attempt is None:
                raise RuntimeError(f"Provider attempt {attempt_id} does not exist")
            attempt.status = status
            attempt.status_code = status_code
            attempt.duration_ms = duration_ms
            attempt.error_code = error_code
            attempt.completed_at = datetime.now(timezone.utc)
            await session.commit()
