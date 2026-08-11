import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.exceptions import ModelNotAvailableError
from app.main import create_app
from app.models import RequestLog
from app.routing import ProviderRoute, ProviderRouter
from tests.conftest import FakeProvider


def route(name, provider, models=("*",), priority=100, enabled=True):
    return ProviderRoute(
        name=name,
        provider=provider,
        models=models,
        priority=priority,
        enabled=enabled,
    )


def test_router_selects_provider_that_supports_model():
    openai = FakeProvider()
    deepseek = FakeProvider()
    router = ProviderRouter(
        [
            route("openai", openai, ("gpt-4.1-mini",), 10),
            route("deepseek", deepseek, ("deepseek-chat",), 20),
        ]
    )

    assert router.select("deepseek-chat").provider is deepseek


def test_router_uses_lowest_priority_number_deterministically():
    preferred = FakeProvider()
    secondary = FakeProvider()
    router = ProviderRouter(
        [
            route("secondary", secondary, ("shared-model",), 20),
            route("preferred", preferred, ("shared-model",), 10),
        ]
    )

    assert router.select("shared-model").name == "preferred"


def test_router_supports_wildcard_and_ignores_disabled_provider():
    disabled = FakeProvider()
    fallback = FakeProvider()
    router = ProviderRouter(
        [
            route("disabled", disabled, ("target-model",), 1, enabled=False),
            route("fallback", fallback, ("*",), 50),
        ]
    )

    assert router.select("target-model").name == "fallback"
    assert router.provider_names == ("fallback",)


def test_router_rejects_unknown_model():
    router = ProviderRouter([route("openai", FakeProvider(), ("known-model",))])

    with pytest.raises(ModelNotAvailableError):
        router.select("unknown-model")


def test_router_rejects_duplicate_names():
    with pytest.raises(ValueError, match="unique"):
        ProviderRouter(
            [
                route("duplicate", FakeProvider()),
                route("duplicate", FakeProvider()),
            ]
        )


def test_selected_provider_is_called_and_persisted(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    first = FakeProvider()
    selected = FakeProvider()
    router = ProviderRouter(
        [
            route("first", first, ("first-model",), 10),
            route("selected", selected, ("selected-model",), 20),
        ]
    )
    app = create_app(
        settings=settings,
        provider_router=router,
        database=database,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "selected-model",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    async def inspect():
        async with database.sessions() as session:
            result = await session.execute(select(RequestLog))
            return result.scalar_one().provider_name

    assert response.status_code == 200
    assert first.last_payload is None
    assert selected.last_payload["model"] == "selected-model"
    assert asyncio.run(inspect()) == "selected"
