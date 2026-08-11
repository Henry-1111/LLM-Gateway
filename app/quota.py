import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Protocol

from redis.exceptions import RedisError

from app.auth import Principal
from app.exceptions import QuotaExceededError, QuotaUnavailableError
from app.metrics import QUOTA_REJECTIONS


RESERVE_QUOTA_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('SET', KEYS[1], ARGV[1])
end

local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', ARGV[4])
for _, reservation_id in ipairs(expired) do
  local expired_amount = redis.call('HGET', KEYS[2], reservation_id)
  if expired_amount then
    redis.call('INCRBY', KEYS[1], expired_amount)
    redis.call('HDEL', KEYS[2], reservation_id)
  end
  redis.call('ZREM', KEYS[3], reservation_id)
end

local existing = redis.call('HGET', KEYS[2], ARGV[3])
if existing then
  return {2, tonumber(redis.call('GET', KEYS[1])), tonumber(existing)}
end

local balance = tonumber(redis.call('GET', KEYS[1]))
local requested = tonumber(ARGV[2])
if balance < requested then
  return {0, balance, 0}
end

local remaining = redis.call('DECRBY', KEYS[1], requested)
redis.call('HSET', KEYS[2], ARGV[3], requested)
redis.call('ZADD', KEYS[3], ARGV[5], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[6])
redis.call('EXPIRE', KEYS[2], ARGV[6])
redis.call('EXPIRE', KEYS[3], ARGV[6])
return {1, tonumber(remaining), requested}
"""


COMMIT_QUOTA_LUA = """
local reserved = redis.call('HGET', KEYS[2], ARGV[1])
if not reserved then
  return {0, tonumber(redis.call('GET', KEYS[1]) or '0'), 0}
end
local delta = tonumber(reserved) - tonumber(ARGV[2])
local remaining = redis.call('INCRBY', KEYS[1], delta)
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return {1, tonumber(remaining), tonumber(reserved)}
"""


RELEASE_QUOTA_LUA = """
local reserved = redis.call('HGET', KEYS[2], ARGV[1])
if not reserved then
  return {0, tonumber(redis.call('GET', KEYS[1]) or '0'), 0}
end
local remaining = redis.call('INCRBY', KEYS[1], reserved)
redis.call('HDEL', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return {1, tonumber(remaining), tonumber(reserved)}
"""


class RedisProtocol(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args): ...


@dataclass(frozen=True)
class QuotaReservation:
    reservation_id: str
    tenant_id: str
    quota_version: int
    limit: int
    reserved_tokens: int
    remaining_tokens: int
    period: str


@dataclass(frozen=True)
class QuotaSettlement:
    limit: int
    remaining_tokens: int
    period: str

    def headers(self) -> Dict[str, str]:
        return {
            "X-Quota-Limit": str(self.limit),
            "X-Quota-Remaining": str(self.remaining_tokens),
            "X-Quota-Period": self.period,
        }


class QuotaManagerProtocol(Protocol):
    async def reserve(
        self,
        principal: Principal,
        reservation_id: str,
        requested_tokens: int,
    ) -> Optional[QuotaReservation]: ...

    async def commit(
        self,
        reservation: QuotaReservation,
        actual_tokens: Optional[int],
    ) -> Optional[QuotaSettlement]: ...

    async def release(
        self,
        reservation: QuotaReservation,
    ) -> Optional[QuotaSettlement]: ...


class NoopQuotaManager:
    async def reserve(self, principal, reservation_id, requested_tokens):
        return None

    async def commit(self, reservation, actual_tokens):
        return None

    async def release(self, reservation):
        return None


class RedisQuotaManager:
    def __init__(
        self,
        redis: RedisProtocol,
        default_limit: int,
        reservation_ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._default_limit = default_limit
        self._reservation_ttl_seconds = reservation_ttl_seconds

    async def reserve(
        self,
        principal: Principal,
        reservation_id: str,
        requested_tokens: int,
    ) -> QuotaReservation:
        now = datetime.now(timezone.utc)
        period = now.strftime("%Y-%m")
        limit = principal.token_quota_limit or self._default_limit
        keys = self._keys(principal.tenant_id, period, principal.quota_version)
        deadline = int(now.timestamp()) + self._reservation_ttl_seconds
        key_ttl = self._period_ttl(now)
        try:
            result = await self._redis.eval(
                RESERVE_QUOTA_LUA,
                3,
                *keys,
                limit,
                requested_tokens,
                reservation_id,
                int(now.timestamp()),
                deadline,
                key_ttl,
            )
            allowed, remaining, reserved = map(int, result)
        except (RedisError, TypeError, ValueError, IndexError) as exc:
            raise QuotaUnavailableError() from exc

        if allowed == 0:
            QUOTA_REJECTIONS.inc()
            raise QuotaExceededError(
                limit=limit,
                remaining=remaining,
                required=requested_tokens,
                period=period,
            )
        return QuotaReservation(
            reservation_id=reservation_id,
            tenant_id=principal.tenant_id,
            quota_version=principal.quota_version,
            limit=limit,
            reserved_tokens=reserved,
            remaining_tokens=remaining,
            period=period,
        )

    async def commit(
        self,
        reservation: QuotaReservation,
        actual_tokens: Optional[int],
    ) -> QuotaSettlement:
        charged_tokens = (
            actual_tokens
            if isinstance(actual_tokens, int) and actual_tokens >= 0
            else reservation.reserved_tokens
        )
        remaining = await self._finish(
            COMMIT_QUOTA_LUA,
            reservation,
            charged_tokens,
        )
        return QuotaSettlement(reservation.limit, remaining, reservation.period)

    async def release(self, reservation: QuotaReservation) -> QuotaSettlement:
        remaining = await self._finish(RELEASE_QUOTA_LUA, reservation)
        return QuotaSettlement(reservation.limit, remaining, reservation.period)

    async def _finish(
        self,
        script: str,
        reservation: QuotaReservation,
        actual_tokens: Optional[int] = None,
    ) -> int:
        keys = self._keys_from_reservation(reservation)
        arguments = [reservation.reservation_id]
        if actual_tokens is not None:
            arguments.append(actual_tokens)
        try:
            result = await self._redis.eval(script, 3, *keys, *arguments)
            completed, remaining = int(result[0]), int(result[1])
        except (RedisError, TypeError, ValueError, IndexError) as exc:
            raise QuotaUnavailableError() from exc
        if completed != 1:
            raise QuotaUnavailableError()
        return remaining

    @staticmethod
    def estimate_tokens(payload: Dict[str, Any], completion_reserve: int) -> int:
        prompt_bytes = len(
            json.dumps(
                payload.get("messages", []),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        completion_tokens = payload.get("max_tokens")
        if not isinstance(completion_tokens, int) or completion_tokens <= 0:
            completion_tokens = completion_reserve
        return max(prompt_bytes + completion_tokens, 1)

    @staticmethod
    def _keys(tenant_id: str, period: str, quota_version: int):
        prefix = f"quota:{tenant_id}:{period}:v{quota_version}"
        return (
            f"{prefix}:balance",
            f"{prefix}:reservations",
            f"{prefix}:deadlines",
        )

    @staticmethod
    def _keys_from_reservation(reservation: QuotaReservation):
        return RedisQuotaManager._keys(
            reservation.tenant_id,
            reservation.period,
            reservation.quota_version,
        )

    @staticmethod
    def _period_ttl(now: datetime) -> int:
        if now.month == 12:
            next_month = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
        return int((next_month + timedelta(days=1) - now).total_seconds())
