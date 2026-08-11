import asyncio

from sqlalchemy import select

from app.auth import ApiKeyService, hash_api_key
from app.exceptions import UpstreamTimeoutError
from app.main import create_app
from app.models import ApiKey, RequestLog


def test_api_key_is_hashed_and_authenticates(database, tenant_api_key):
    tenant, created = tenant_api_key

    async def inspect():
        async with database.sessions() as session:
            stored = await session.get(ApiKey, created.id)
            assert stored.key_hash == hash_api_key(created.plaintext)
            assert stored.key_hash != created.plaintext
            assert stored.key_prefix == created.plaintext[:14]

        principal = await ApiKeyService(database.sessions).authenticate(created.plaintext)
        assert principal.tenant_id == tenant.id
        assert principal.api_key_id == created.id

    asyncio.run(inspect())


def test_updating_tenant_quota_rotates_quota_version(database, tenant_api_key):
    tenant, _ = tenant_api_key

    async def update_and_authenticate():
        service = ApiKeyService(database.sessions)
        updated = await service.set_tenant_quota(tenant.id, 250000)
        principal = await service.authenticate(tenant_api_key[1].plaintext)
        return updated, principal

    updated, principal = asyncio.run(update_and_authenticate())
    assert updated.token_quota_limit == 250000
    assert updated.quota_version == 2
    assert principal.token_quota_limit == 250000
    assert principal.quota_version == 2


def test_successful_chat_is_persisted(
    client,
    database,
    tenant_api_key,
    auth_headers,
):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )

    async def inspect():
        async with database.sessions() as session:
            result = await session.execute(select(RequestLog))
            request_log = result.scalar_one()
            assert request_log.id == response.headers["x-request-id"]
            assert request_log.tenant_id == tenant_api_key[0].id
            assert request_log.api_key_id == tenant_api_key[1].id
            assert request_log.provider_name == "injected-provider"
            assert request_log.status == "succeeded"
            assert request_log.total_tokens == 6
            assert request_log.completed_at is not None

    assert response.status_code == 200
    asyncio.run(inspect())


def test_failed_chat_is_persisted(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    from fastapi.testclient import TestClient

    from tests.conftest import FakeProvider

    app = create_app(
        settings=settings,
        provider=FakeProvider(error=UpstreamTimeoutError()),
        database=database,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    async def inspect():
        async with database.sessions() as session:
            result = await session.execute(select(RequestLog))
            request_log = result.scalar_one()
            assert request_log.status == "failed"
            assert request_log.status_code == 504
            assert request_log.error_code == "upstream_timeout"
            assert request_log.total_tokens == 0

    assert response.status_code == 504
    asyncio.run(inspect())
