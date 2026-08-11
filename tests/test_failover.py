import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.exceptions import UpstreamResponseError, UpstreamTimeoutError
from app.main import create_app
from app.models import ProviderAttempt, RequestLog
from app.routing import ProviderRoute, ProviderRouter
from tests.conftest import FakeProvider


def make_router(*providers):
    return ProviderRouter(
        ProviderRoute(
            name=name,
            provider=provider,
            models=("shared-model",),
            priority=index * 10,
        )
        for index, (name, provider) in enumerate(providers, start=1)
    )


def send(client, auth_headers):
    return client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "shared-model",
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )


def read_attempts(database):
    async def inspect():
        async with database.sessions() as session:
            result = await session.execute(
                select(ProviderAttempt).order_by(ProviderAttempt.attempt_number)
            )
            attempts = result.scalars().all()
            request_log = (await session.execute(select(RequestLog))).scalar_one()
            return attempts, request_log

    return asyncio.run(inspect())


def test_timeout_fails_over_to_next_provider(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    primary = FakeProvider(error=UpstreamTimeoutError())
    secondary = FakeProvider()
    app = create_app(
        settings=settings,
        provider_router=make_router(("primary", primary), ("secondary", secondary)),
        database=database,
    )

    with TestClient(app) as client:
        response = send(client, auth_headers)

    attempts, request_log = read_attempts(database)
    assert response.status_code == 200
    assert response.headers["x-provider"] == "secondary"
    assert response.headers["x-provider-attempts"] == "2"
    assert primary.call_count == 1
    assert secondary.call_count == 1
    assert [(item.provider_name, item.status) for item in attempts] == [
        ("primary", "failed"),
        ("secondary", "succeeded"),
    ]
    assert attempts[0].error_code == "upstream_timeout"
    assert request_log.status == "succeeded"
    assert request_log.provider_name == "secondary"


def test_non_retryable_provider_error_does_not_fail_over(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    primary = FakeProvider(error=UpstreamResponseError(400, "Invalid request"))
    secondary = FakeProvider()
    app = create_app(
        settings=settings,
        provider_router=make_router(("primary", primary), ("secondary", secondary)),
        database=database,
    )

    with TestClient(app) as client:
        response = send(client, auth_headers)

    attempts, request_log = read_attempts(database)
    assert response.status_code == 502
    assert primary.call_count == 1
    assert secondary.call_count == 0
    assert len(attempts) == 1
    assert request_log.status == "failed"


def test_final_error_is_returned_when_all_providers_fail(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    primary = FakeProvider(error=UpstreamTimeoutError())
    secondary = FakeProvider(error=UpstreamResponseError(503, "Unavailable"))
    app = create_app(
        settings=settings,
        provider_router=make_router(("primary", primary), ("secondary", secondary)),
        database=database,
    )

    with TestClient(app) as client:
        response = send(client, auth_headers)

    attempts, request_log = read_attempts(database)
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Unavailable"
    assert len(attempts) == 2
    assert all(item.status == "failed" for item in attempts)
    assert request_log.status == "failed"
    assert request_log.provider_name == "secondary"


def test_max_provider_attempts_stops_additional_calls(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    limited_settings = settings.model_copy(update={"max_provider_attempts": 1})
    primary = FakeProvider(error=UpstreamTimeoutError())
    secondary = FakeProvider()
    app = create_app(
        settings=limited_settings,
        provider_router=make_router(("primary", primary), ("secondary", secondary)),
        database=database,
    )

    with TestClient(app) as client:
        response = send(client, auth_headers)

    attempts, _ = read_attempts(database)
    assert response.status_code == 504
    assert primary.call_count == 1
    assert secondary.call_count == 0
    assert len(attempts) == 1
