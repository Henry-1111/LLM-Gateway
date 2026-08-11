from fastapi.testclient import TestClient

from loadtest.mock_provider import app


def test_mock_provider_returns_openai_compatible_usage(monkeypatch):
    monkeypatch.setenv("MOCK_PROVIDER_DELAY_MS", "0")
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "load-test-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "load-test-model"
    assert body["usage"]["total_tokens"] == (
        body["usage"]["prompt_tokens"] + body["usage"]["completion_tokens"]
    )


def test_mock_provider_health():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
