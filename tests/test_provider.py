import httpx
import pytest

from app.exceptions import UpstreamResponseError, UpstreamTimeoutError
from app.provider import OpenAICompatibleProvider


def make_provider(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return OpenAICompatibleProvider(
        client=client,
        api_key="provider-key",
        base_url="https://provider.example/v1",
        timeout_seconds=1,
    ), client


@pytest.mark.asyncio
async def test_provider_sends_openai_compatible_request():
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(
            200,
            json={"choices": [], "usage": {"total_tokens": 0}},
        )

    provider, client = make_provider(handler)
    try:
        result = await provider.create_chat_completion(
            {"model": "test-model", "messages": []}
        )
    finally:
        await client.aclose()

    request = captured["request"]
    assert request.url == "https://provider.example/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer provider-key"
    assert result["choices"] == []


@pytest.mark.asyncio
async def test_provider_maps_timeout():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    provider, client = make_provider(handler)
    try:
        with pytest.raises(UpstreamTimeoutError):
            await provider.create_chat_completion({"model": "test-model"})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_maps_error_response():
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "Provider failed"}})

    provider, client = make_provider(handler)
    try:
        with pytest.raises(UpstreamResponseError) as captured:
            await provider.create_chat_completion({"model": "test-model"})
    finally:
        await client.aclose()

    assert captured.value.upstream_status_code == 500
    assert captured.value.message == "Provider failed"


@pytest.mark.asyncio
async def test_provider_rejects_invalid_json():
    def handler(request):
        return httpx.Response(200, text="not-json")

    provider, client = make_provider(handler)
    try:
        with pytest.raises(UpstreamResponseError) as captured:
            await provider.create_chat_completion({"model": "test-model"})
    finally:
        await client.aclose()

    assert captured.value.message == "The model provider returned invalid JSON."
