import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderSettings(BaseModel):
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    base_url: str
    models: List[str] = Field(default_factory=lambda: ["*"])
    priority: int = 100
    enabled: bool = True
    timeout_seconds: float = Field(default=30.0, gt=0)


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables or .env."""

    provider_api_key: str = ""
    provider_base_url: str = "https://api.openai.com/v1"
    default_model: str = "gpt-4.1-mini"
    provider_timeout_seconds: float = Field(default=30.0, gt=0)
    max_provider_attempts: int = Field(default=2, ge=1, le=10)
    idempotency_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    redis_url: str = "redis://127.0.0.1:6379/0"
    rate_limit_enabled: bool = False
    rate_limit_requests: int = Field(default=60, ge=1, le=1000000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=86400)
    quota_enabled: bool = False
    quota_default_tokens: int = Field(default=1000000, ge=1)
    quota_default_completion_reserve_tokens: int = Field(default=1024, ge=1)
    quota_reservation_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    rabbitmq_url: str = "amqp://guest:guest@127.0.0.1:5672/"
    rabbitmq_exchange: str = "llm_gateway.events"
    outbox_publisher_enabled: bool = False
    outbox_poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=60)
    outbox_batch_size: int = Field(default=100, ge=1, le=1000)
    outbox_max_retry_seconds: int = Field(default=60, ge=1, le=3600)
    event_consumer_enabled: bool = False
    rabbitmq_consumer_queue: str = "llm_gateway.audit"
    rabbitmq_retry_delay_ms: int = Field(default=5000, ge=100, le=3600000)
    rabbitmq_max_retries: int = Field(default=3, ge=0, le=100)
    providers: List[ProviderSettings] = Field(default_factory=list)
    database_url: str = "sqlite+aiosqlite:///./llm_gateway.db"
    database_auto_create: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **values: Any) -> None:
        secret_fields = {
            "provider_api_key": "PROVIDER_API_KEY_FILE",
            "providers": "PROVIDERS_FILE",
            "database_url": "DATABASE_URL_FILE",
            "redis_url": "REDIS_URL_FILE",
            "rabbitmq_url": "RABBITMQ_URL_FILE",
        }
        loaded: Dict[str, Any] = {}
        for field_name, env_name in secret_fields.items():
            if field_name in values:
                continue
            secret_path = os.getenv(env_name)
            if not secret_path:
                continue
            try:
                secret_value = Path(secret_path).read_text(encoding="utf-8").rstrip(
                    "\r\n"
                )
            except OSError as exc:
                raise ValueError(
                    f"Secret file configured by {env_name} is not readable"
                ) from exc
            if not secret_value:
                raise ValueError(f"Secret file configured by {env_name} is empty")
            if field_name == "providers":
                try:
                    loaded[field_name] = json.loads(secret_value)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "Secret file configured by PROVIDERS_FILE must contain JSON"
                    ) from exc
            else:
                loaded[field_name] = secret_value
        super().__init__(**loaded, **values)


@lru_cache
def get_settings() -> Settings:
    return Settings()
