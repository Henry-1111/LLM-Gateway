import asyncio
from collections import defaultdict

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.auth import Principal
from app.exceptions import QuotaExceededError, UpstreamTimeoutError
from app.main import create_app
from app.quota import (
    COMMIT_QUOTA_LUA,
    RELEASE_QUOTA_LUA,
    RESERVE_QUOTA_LUA,
    RedisQuotaManager,
)
from tests.conftest import FakeProvider


class FakeQuotaRedis:
    def __init__(self, error=None):
        self.balances = {}
        self.reservations = defaultdict(dict)
        self.deadlines = defaultdict(dict)
        self.error = error
        self.reserve_calls = 0
        self.commit_calls = 0
        self.release_calls = 0

    async def eval(self, script, numkeys, *args):
        if self.error:
            raise self.error
        balance_key, reservations_key, deadlines_key = args[:3]
        if script == RESERVE_QUOTA_LUA:
            self.reserve_calls += 1
            limit = int(args[3])
            requested = int(args[4])
            reservation_id = args[5]
            now = int(args[6])
            deadline = int(args[7])
            self.balances.setdefault(balance_key, limit)
            for expired_id, expires_at in list(self.deadlines[deadlines_key].items()):
                if expires_at <= now:
                    amount = self.reservations[reservations_key].pop(expired_id, 0)
                    self.balances[balance_key] += amount
                    del self.deadlines[deadlines_key][expired_id]
            if reservation_id in self.reservations[reservations_key]:
                existing = self.reservations[reservations_key][reservation_id]
                return [2, self.balances[balance_key], existing]
            if self.balances[balance_key] < requested:
                return [0, self.balances[balance_key], 0]
            self.balances[balance_key] -= requested
            self.reservations[reservations_key][reservation_id] = requested
            self.deadlines[deadlines_key][reservation_id] = deadline
            return [1, self.balances[balance_key], requested]

        reservation_id = args[3]
        reserved = self.reservations[reservations_key].get(reservation_id)
        if reserved is None:
            return [0, self.balances.get(balance_key, 0), 0]
        if script == COMMIT_QUOTA_LUA:
            self.commit_calls += 1
            actual = int(args[4])
            self.balances[balance_key] += reserved - actual
        elif script == RELEASE_QUOTA_LUA:
            self.release_calls += 1
            self.balances[balance_key] += reserved
        else:
            raise AssertionError("Unexpected Lua script")
        del self.reservations[reservations_key][reservation_id]
        self.deadlines[deadlines_key].pop(reservation_id, None)
        return [1, self.balances[balance_key], reserved]


def principal(limit=None):
    return Principal("tenant-a", "key-a", token_quota_limit=limit)


def test_reserve_and_commit_refunds_unused_tokens():
    redis = FakeQuotaRedis()
    manager = RedisQuotaManager(redis, default_limit=100, reservation_ttl_seconds=300)

    reservation = asyncio.run(manager.reserve(principal(), "request-1", 80))
    settlement = asyncio.run(manager.commit(reservation, actual_tokens=25))

    assert reservation.remaining_tokens == 20
    assert settlement.remaining_tokens == 75
    assert redis.reserve_calls == 1
    assert redis.commit_calls == 1


def test_failed_request_releases_full_reservation():
    redis = FakeQuotaRedis()
    manager = RedisQuotaManager(redis, default_limit=100, reservation_ttl_seconds=300)

    reservation = asyncio.run(manager.reserve(principal(), "request-1", 80))
    settlement = asyncio.run(manager.release(reservation))

    assert settlement.remaining_tokens == 100
    assert redis.release_calls == 1


def test_reservation_is_rejected_when_balance_is_insufficient():
    redis = FakeQuotaRedis()
    manager = RedisQuotaManager(redis, default_limit=100, reservation_ttl_seconds=300)
    asyncio.run(manager.reserve(principal(), "request-1", 80))

    with pytest.raises(QuotaExceededError) as captured:
        asyncio.run(manager.reserve(principal(), "request-2", 30))

    assert captured.value.headers["X-Quota-Remaining"] == "20"
    assert captured.value.headers["X-Quota-Required"] == "30"


def test_tenant_specific_limit_overrides_default():
    redis = FakeQuotaRedis()
    manager = RedisQuotaManager(redis, default_limit=1000, reservation_ttl_seconds=300)

    reservation = asyncio.run(manager.reserve(principal(limit=50), "request-1", 20))

    assert reservation.limit == 50
    assert reservation.remaining_tokens == 30


def test_expired_orphan_reservation_is_reclaimed_before_new_reservation():
    redis = FakeQuotaRedis()
    manager = RedisQuotaManager(redis, default_limit=100, reservation_ttl_seconds=300)
    first = asyncio.run(manager.reserve(principal(), "request-1", 80))
    deadline_key = f"quota:tenant-a:{first.period}:v1:deadlines"
    redis.deadlines[deadline_key]["request-1"] = 0

    second = asyncio.run(manager.reserve(principal(), "request-2", 30))

    assert second.remaining_tokens == 70


def test_successful_endpoint_charges_actual_provider_usage(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    redis = FakeQuotaRedis()
    quota = RedisQuotaManager(redis, default_limit=1000, reservation_ttl_seconds=300)
    quota_settings = settings.model_copy(
        update={"quota_default_completion_reserve_tokens": 20}
    )
    app = create_app(
        settings=quota_settings,
        provider=provider,
        database=database,
        quota_manager=quota,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 200
    assert response.headers["x-quota-limit"] == "1000"
    assert response.headers["x-quota-remaining"] == "994"
    assert redis.commit_calls == 1
    assert provider.call_count == 1
    assert provider.last_payload["max_tokens"] == 20


def test_provider_failure_releases_reserved_quota(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    redis = FakeQuotaRedis()
    quota = RedisQuotaManager(redis, default_limit=1000, reservation_ttl_seconds=300)
    provider = FakeProvider(error=UpstreamTimeoutError())
    quota_settings = settings.model_copy(
        update={"quota_default_completion_reserve_tokens": 20}
    )
    app = create_app(
        settings=quota_settings,
        provider=provider,
        database=database,
        quota_manager=quota,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    balance = next(iter(redis.balances.values()))
    assert response.status_code == 504
    assert balance == 1000
    assert redis.release_calls == 1


def test_quota_exceeded_prevents_provider_call(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    quota = RedisQuotaManager(
        FakeQuotaRedis(),
        default_limit=10,
        reservation_ttl_seconds=300,
    )
    app = create_app(
        settings=settings,
        provider=provider,
        database=database,
        quota_manager=quota,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    assert provider.call_count == 0


def test_idempotency_replay_does_not_reserve_quota_twice(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    redis = FakeQuotaRedis()
    quota = RedisQuotaManager(redis, default_limit=1000, reservation_ttl_seconds=300)
    quota_settings = settings.model_copy(
        update={"quota_default_completion_reserve_tokens": 20}
    )
    app = create_app(
        settings=quota_settings,
        provider=provider,
        database=database,
        quota_manager=quota,
    )
    headers = {**auth_headers, "Idempotency-Key": "quota-idempotency-key"}

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

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert redis.reserve_calls == 1
    assert redis.commit_calls == 1


def test_quota_redis_failure_returns_service_unavailable(
    settings,
    provider,
    database,
    tenant_api_key,
    auth_headers,
):
    quota = RedisQuotaManager(
        FakeQuotaRedis(error=ConnectionError("redis unavailable")),
        default_limit=1000,
        reservation_ttl_seconds=300,
    )
    app = create_app(
        settings=settings,
        provider=provider,
        database=database,
        quota_manager=quota,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "quota_unavailable"
    assert provider.call_count == 0
