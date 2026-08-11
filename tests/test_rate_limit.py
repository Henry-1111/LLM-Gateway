from collections import defaultdict

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.auth import Principal
from app.main import create_app
from app.rate_limit import FIXED_WINDOW_LUA, RedisRateLimiter


class FakeRedis:
    def __init__(self, error=None):
        self.counts = defaultdict(int)
        self.error = error
        self.calls = []

    async def eval(self, script, numkeys, key, window_seconds):
        self.calls.append((script, numkeys, key, window_seconds))
        if self.error:
            raise self.error
        self.counts[key] += 1
        return [self.counts[key], int(window_seconds)]


def test_rate_limiter_uses_tenant_and_api_key_scoped_redis_key():
    redis = FakeRedis()
    limiter = RedisRateLimiter(redis, limit=2, window_seconds=60)
    first = Principal(tenant_id="tenant-a", api_key_id="key-a")
    second = Principal(tenant_id="tenant-b", api_key_id="key-a")

    import asyncio

    first_result = asyncio.run(limiter.check(first))
    second_result = asyncio.run(limiter.check(second))

    assert first_result.remaining == 1
    assert second_result.remaining == 1
    assert redis.calls[0][0] == FIXED_WINDOW_LUA
    assert redis.calls[0][1:] == (1, "rate_limit:tenant-a:key-a", 60)


def test_endpoint_rejects_request_after_limit(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    limiter = RedisRateLimiter(FakeRedis(), limit=2, window_seconds=60)
    app = create_app(
        settings=settings,
        provider=provider,
        database=database,
        rate_limiter=limiter,
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "One"}]},
        )
        second = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Two"}]},
        )
        rejected = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Three"}]},
        )

    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "2"
    assert first.headers["x-ratelimit-remaining"] == "1"
    assert second.status_code == 200
    assert second.headers["x-ratelimit-remaining"] == "0"
    assert rejected.status_code == 429
    assert rejected.json()["error"]["code"] == "rate_limit_exceeded"
    assert rejected.headers["x-ratelimit-limit"] == "2"
    assert rejected.headers["x-ratelimit-remaining"] == "0"
    assert int(rejected.headers["retry-after"]) >= 1
    assert provider.call_count == 2


def test_idempotency_replay_still_consumes_rate_limit(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    limiter = RedisRateLimiter(FakeRedis(), limit=2, window_seconds=60)
    app = create_app(
        settings=settings,
        provider=provider,
        database=database,
        rate_limiter=limiter,
    )
    headers = {**auth_headers, "Idempotency-Key": "rate-limited-idempotency"}

    with TestClient(app) as client:
        first = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        replay = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )
        rejected = client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.headers["x-ratelimit-remaining"] == "0"
    assert rejected.status_code == 429
    assert provider.call_count == 1


def test_redis_failure_returns_service_unavailable(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    limiter = RedisRateLimiter(
        FakeRedis(error=ConnectionError("redis unavailable")),
        limit=2,
        window_seconds=60,
    )
    app = create_app(
        settings=settings,
        provider=provider,
        database=database,
        rate_limiter=limiter,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "rate_limit_unavailable"
    assert provider.call_count == 0
