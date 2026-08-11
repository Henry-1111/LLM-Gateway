import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.exceptions import UpstreamTimeoutError
from app.idempotency import IdempotencyService, request_fingerprint
from app.main import create_app
from app.models import IdempotencyRecord, ProviderAttempt, RequestLog
from tests.conftest import FakeProvider


IDEMPOTENCY_KEY = "chat-operation-0001"


def send(client, auth_headers, content="Hi", key=IDEMPOTENCY_KEY):
    headers = {**auth_headers, "Idempotency-Key": key}
    return client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": content}]},
    )


def test_successful_request_is_replayed_without_second_provider_call(
    client,
    provider,
    auth_headers,
    database,
):
    first = send(client, auth_headers)
    second = send(client, auth_headers)

    async def inspect():
        async with database.sessions() as session:
            request_count = await session.scalar(select(func.count(RequestLog.id)))
            attempt_count = await session.scalar(select(func.count(ProviderAttempt.id)))
            record = (await session.execute(select(IdempotencyRecord))).scalar_one()
            return request_count, attempt_count, record

    request_count, attempt_count, record = asyncio.run(inspect())
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert first.headers["x-request-id"] == second.headers["x-request-id"]
    assert first.headers["idempotency-replayed"] == "false"
    assert second.headers["idempotency-replayed"] == "true"
    assert provider.call_count == 1
    assert request_count == 1
    assert attempt_count == 1
    assert record.status == "completed"


def test_failed_request_is_replayed_without_second_provider_call(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    provider = FakeProvider(error=UpstreamTimeoutError())
    app = create_app(settings=settings, provider=provider, database=database)

    with TestClient(app) as client:
        first = send(client, auth_headers)
        second = send(client, auth_headers)

    async def inspect():
        async with database.sessions() as session:
            record = (await session.execute(select(IdempotencyRecord))).scalar_one()
            return record.status

    assert first.status_code == 504
    assert second.status_code == 504
    assert first.json() == second.json()
    assert second.headers["idempotency-replayed"] == "true"
    assert provider.call_count == 1
    assert asyncio.run(inspect()) == "failed"


def test_reusing_key_with_different_request_returns_conflict(
    client,
    provider,
    auth_headers,
):
    first = send(client, auth_headers, content="First")
    second = send(client, auth_headers, content="Different")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
    assert provider.call_count == 1


def test_processing_request_returns_in_progress(
    client,
    database,
    tenant_api_key,
    auth_headers,
    settings,
):
    tenant, _ = tenant_api_key
    fingerprint = request_fingerprint(
        {
            "endpoint": "/v1/chat/completions",
            "body": {
                "model": settings.default_model,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
        }
    )
    service = IdempotencyService(database.sessions, settings.idempotency_ttl_seconds)
    asyncio.run(service.begin(tenant.id, IDEMPOTENCY_KEY, fingerprint))

    response = send(client, auth_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_in_progress"


def test_short_idempotency_key_is_rejected(client, provider, auth_headers):
    response = send(client, auth_headers, key="short")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert provider.call_count == 0
