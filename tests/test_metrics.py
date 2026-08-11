from prometheus_client import REGISTRY


def sample_value(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_exposes_prometheus_text(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "gateway_http_requests_total" in response.text
    assert "gateway_provider_attempts_total" in response.text
    assert "gateway_tokens_total" in response.text
    assert "gateway_outbox_publish_failures_total" in response.text
    assert "gateway_consumer_retries_total" in response.text


def test_http_middleware_counts_route_template(client):
    labels = {"method": "GET", "route": "/health", "status_code": "200"}
    before = sample_value("gateway_http_requests_total", labels)

    response = client.get("/health")

    after = sample_value("gateway_http_requests_total", labels)
    assert response.status_code == 200
    assert after - before == 1


def test_successful_provider_call_records_attempt_duration_and_tokens(
    client,
    auth_headers,
):
    attempt_labels = {
        "provider": "injected-provider",
        "outcome": "succeeded",
        "error_code": "none",
    }
    token_labels = {"provider": "injected-provider", "token_type": "total"}
    before_attempts = sample_value("gateway_provider_attempts_total", attempt_labels)
    before_tokens = sample_value("gateway_tokens_total", token_labels)

    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )

    after_attempts = sample_value("gateway_provider_attempts_total", attempt_labels)
    after_tokens = sample_value("gateway_tokens_total", token_labels)
    duration_count = sample_value(
        "gateway_provider_duration_seconds_count",
        {"provider": "injected-provider", "outcome": "succeeded"},
    )
    assert response.status_code == 200
    assert after_attempts - before_attempts == 1
    assert after_tokens - before_tokens == 6
    assert duration_count >= 1


def test_metrics_do_not_expose_high_cardinality_identifiers(
    client,
    auth_headers,
    tenant_api_key,
):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )
    request_id = response.headers["x-request-id"]
    tenant, api_key = tenant_api_key

    metrics = client.get("/metrics").text

    assert request_id not in metrics
    assert tenant.id not in metrics
    assert api_key.id not in metrics
    assert api_key.plaintext not in metrics
    assert "request_id=" not in metrics
    assert "tenant_id=" not in metrics
    assert "api_key_id=" not in metrics
