import asyncio
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

from app.auth import ApiKeyService
from app.config import Settings
from app.database import Database
from app.main import create_app


class FakeProvider:
    def __init__(
        self,
        response: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
    ) -> None:
        self.response = response or {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        }
        self.error = error
        self.last_payload: Optional[Dict[str, Any]] = None
        self.call_count = 0

    async def create_chat_completion(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.call_count += 1
        self.last_payload = payload
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def settings() -> Settings:
    return Settings(
        provider_api_key="provider-test-key",
        default_model="test-model",
        database_auto_create=False,
    )


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def database(tmp_path) -> Database:
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    asyncio.run(database.create_schema())
    yield database
    asyncio.run(database.close())


@pytest.fixture
def tenant_api_key(database: Database):
    async def seed():
        service = ApiKeyService(database.sessions)
        tenant = await service.create_tenant("Test tenant")
        key = await service.create_api_key(tenant.id, "Test key")
        return tenant, key

    return asyncio.run(seed())


@pytest.fixture
def client(
    settings: Settings,
    provider: FakeProvider,
    database: Database,
    tenant_api_key,
) -> TestClient:
    with TestClient(
        create_app(settings=settings, provider=provider, database=database)
    ) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(tenant_api_key) -> Dict[str, str]:
    _, api_key = tenant_api_key
    return {"Authorization": f"Bearer {api_key.plaintext}"}
