def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_dependency_status(client):
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok"},
    }


def test_ready_returns_503_when_database_is_unavailable(client, monkeypatch):
    async def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(client.app.state.database, "ping", unavailable)
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "error"},
    }
