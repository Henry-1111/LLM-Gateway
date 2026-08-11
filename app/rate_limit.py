import time
from dataclasses import dataclass
from typing import Optional, Protocol

from redis.exceptions import RedisError

from app.auth import Principal
from app.exceptions import RateLimitExceededError, RateLimitUnavailableError
from app.metrics import RATE_LIMIT_REJECTIONS


FIXED_WINDOW_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {current, ttl}
"""


class RedisProtocol(Protocol):
    async def eval(self, script: str, numkeys: int, *keys_and_args): ...


class RateLimiterProtocol(Protocol):
    async def check(self, principal: Principal) -> Optional["RateLimitDecision"]: ...


@dataclass(frozen=True)
class RateLimitDecision:
    limit: int
    remaining: int
    reset_at: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_at),
        }


class NoopRateLimiter:
    async def check(self, principal: Principal) -> None:
        return None


class RedisRateLimiter:
    def __init__(
        self,
        redis: RedisProtocol,
        limit: int,
        window_seconds: int,
    ) -> None:
        self._redis = redis
        self._limit = limit
        self._window_seconds = window_seconds

    async def check(self, principal: Principal) -> RateLimitDecision:
        key = self._key(principal)
        try:
            result = await self._redis.eval(
                FIXED_WINDOW_LUA,
                1,
                key,
                self._window_seconds,
            )
            current, ttl = int(result[0]), int(result[1])
        except (RedisError, TypeError, ValueError, IndexError) as exc:
            raise RateLimitUnavailableError() from exc

        if ttl < 0:
            ttl = self._window_seconds
        remaining = max(self._limit - current, 0)
        reset_at = int(time.time()) + ttl
        decision = RateLimitDecision(
            limit=self._limit,
            remaining=remaining,
            reset_at=reset_at,
        )
        if current > self._limit:
            RATE_LIMIT_REJECTIONS.inc()
            raise RateLimitExceededError(
                limit=decision.limit,
                remaining=decision.remaining,
                reset_at=decision.reset_at,
                retry_after=max(ttl, 1),
            )
        return decision

    @staticmethod
    def _key(principal: Principal) -> str:
        return f"rate_limit:{principal.tenant_id}:{principal.api_key_id}"
