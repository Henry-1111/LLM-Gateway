import json

import pytest

from app.exceptions import UpstreamResponseError, UpstreamTimeoutError


def test_chat_forwards_request_and_returns_provider_response(
    client, provider, auth_headers
):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hello!"
    assert provider.last_payload == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False,
    }
    assert response.headers["x-request-id"]


def test_chat_requires_gateway_api_key(client):
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_chat_rejects_streaming(client, auth_headers):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "streaming_not_supported"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (UpstreamTimeoutError(), 504, "upstream_timeout"),
        (UpstreamResponseError(429, "Rate limited"), 429, "upstream_error"),
        (UpstreamResponseError(500), 502, "upstream_error"),
    ],
)
def test_chat_maps_provider_errors(
    settings,
    auth_headers,
    error,
    expected_status,
    expected_code,
    database,
):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import FakeProvider

    with TestClient(
        create_app(
            settings=settings,
            provider=FakeProvider(error=error),
            database=database,
        )
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code


def test_success_log_contains_token_usage(client, auth_headers, caplog):
    with caplog.at_level("INFO", logger="llm_gateway.requests"):
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={"messages": [{"role": "user", "content": "Hi"}]},
        )

    gateway_record = next(
        item for item in caplog.records if item.name == "llm_gateway.requests"
    )
    record = json.loads(gateway_record.message)
    assert response.status_code == 200
    assert record["status"] == "succeeded"
    assert record["prompt_tokens"] == 4
    assert record["completion_tokens"] == 2
    assert record["total_tokens"] == 6
