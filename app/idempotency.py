import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InvalidIdempotencyKeyError,
)
from app.models import IdempotencyRecord


@dataclass(frozen=True)
class IdempotencyReservation:
    record_id: str


@dataclass(frozen=True)
class IdempotencyReplay:
    status_code: int
    body: Dict[str, Any]
    headers: Dict[str, str]


IdempotencyDecision = Union[IdempotencyReservation, IdempotencyReplay]


def request_fingerprint(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def hash_idempotency_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class IdempotencyService:
    def __init__(self, sessions: async_sessionmaker, ttl_seconds: int) -> None:
        self._sessions = sessions
        self._ttl_seconds = ttl_seconds

    async def begin(
        self,
        tenant_id: str,
        key: str,
        fingerprint: str,
    ) -> IdempotencyDecision:
        self._validate_key(key)
        key_hash = hash_idempotency_key(key)

        for _ in range(2):
            now = datetime.now(timezone.utc)
            record = IdempotencyRecord(
                tenant_id=tenant_id,
                key_hash=key_hash,
                request_fingerprint=fingerprint,
                status="processing",
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            async with self._sessions() as session:
                session.add(record)
                try:
                    await session.commit()
                    await session.refresh(record)
                    return IdempotencyReservation(record_id=record.id)
                except IntegrityError:
                    await session.rollback()

                result = await session.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.tenant_id == tenant_id,
                        IdempotencyRecord.key_hash == key_hash,
                    )
                )
                existing = result.scalar_one()
                expires_at = existing.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    await session.delete(existing)
                    await session.commit()
                    continue
                return self._existing_decision(existing, fingerprint)

        raise IdempotencyInProgressError()

    async def complete(
        self,
        record_id: str,
        status: str,
        status_code: int,
        body: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        async with self._sessions() as session:
            record = await session.get(IdempotencyRecord, record_id)
            if record is None:
                raise RuntimeError(f"Idempotency record {record_id} does not exist")
            record.status = status
            record.response_status_code = status_code
            record.response_body = json.dumps(body, separators=(",", ":"))
            record.response_headers = json.dumps(headers or {}, separators=(",", ":"))
            record.request_id = request_id
            record.completed_at = datetime.now(timezone.utc)
            await session.commit()

    @staticmethod
    def _validate_key(key: str) -> None:
        if not 8 <= len(key) <= 255 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise InvalidIdempotencyKeyError()

    @staticmethod
    def _existing_decision(
        record: IdempotencyRecord,
        fingerprint: str,
    ) -> IdempotencyDecision:
        if record.request_fingerprint != fingerprint:
            raise IdempotencyConflictError()
        if record.status == "processing":
            raise IdempotencyInProgressError()
        if record.response_status_code is None or record.response_body is None:
            raise IdempotencyInProgressError()
        return IdempotencyReplay(
            status_code=record.response_status_code,
            body=json.loads(record.response_body),
            headers=json.loads(record.response_headers or "{}"),
        )
