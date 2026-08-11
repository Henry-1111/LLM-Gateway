import json

from app.config import Settings


def test_providers_are_loaded_from_json_environment(monkeypatch):
    providers = [
        {
            "name": "primary",
            "api_key": "secret",
            "base_url": "https://provider.example/v1",
            "models": ["model-a"],
            "priority": 10,
            "enabled": True,
            "timeout_seconds": 12,
        }
    ]
    monkeypatch.setenv("PROVIDERS", json.dumps(providers))

    settings = Settings(_env_file=None)

    assert len(settings.providers) == 1
    assert settings.providers[0].name == "primary"
    assert settings.providers[0].models == ["model-a"]
    assert settings.providers[0].timeout_seconds == 12


def test_settings_load_connection_urls_from_secret_files(monkeypatch, tmp_path):
    database_secret = tmp_path / "database_url"
    database_secret.write_text("postgresql+asyncpg://secret\n", encoding="utf-8")
    redis_secret = tmp_path / "redis_url"
    redis_secret.write_text("rediss://secret\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL_FILE", str(database_secret))
    monkeypatch.setenv("REDIS_URL_FILE", str(redis_secret))

    settings = Settings(_env_file=None)

    assert settings.database_url == "postgresql+asyncpg://secret"
    assert settings.redis_url == "rediss://secret"


def test_settings_load_providers_from_json_secret_file(monkeypatch, tmp_path):
    providers_secret = tmp_path / "providers"
    providers_secret.write_text(
        json.dumps(
            [
                {
                    "name": "secret-provider",
                    "api_key": "secret",
                    "base_url": "https://provider.example/v1",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROVIDERS_FILE", str(providers_secret))

    settings = Settings(_env_file=None)

    assert [provider.name for provider in settings.providers] == ["secret-provider"]


def test_settings_reject_empty_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "empty"
    secret.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL_FILE", str(secret))

    try:
        Settings(_env_file=None)
    except ValueError as exc:
        assert "DATABASE_URL_FILE is empty" in str(exc)
    else:
        raise AssertionError("Expected empty secret file to be rejected")
