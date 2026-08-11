import asyncio
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import OutboxEvent
from app.outbox import OutboxProcessor


class FakePublisher:
    def __init__(self, error=None):
        self.error = error
        self.events = []

    async def publish(self, event):
        if self.error:
            raise self.error
        self.events.append(event)

    async def close(self):
        return None


def load_single_event(database):
    async def load():
        async with database.sessions() as session:
            return (await session.execute(select(OutboxEvent))).scalar_one()

    return asyncio.run(load())


def test_successful_request_creates_pending_outbox_event_atomically(
    client,
    auth_headers,
    database,
):
    response = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )

    event = load_single_event(database)
    payload = json.loads(event.payload)
    assert response.status_code == 200
    assert event.status == "pending"
    assert event.event_type == "request.completed"
    assert event.aggregate_id == response.headers["x-request-id"]
    assert payload["event_id"] == event.id
    assert payload["request_id"] == response.headers["x-request-id"]
    assert payload["total_tokens"] == 6
    assert payload["provider_name"] == "injected-provider"


def test_failed_request_creates_failed_event(
    settings,
    database,
    tenant_api_key,
    auth_headers,
):
    from fastapi.testclient import TestClient

    from app.exceptions import UpstreamTimeoutError
    from app.main import create_app
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

    event = load_single_event(database)
    payload = json.loads(event.payload)
    assert response.status_code == 504
    assert event.event_type == "request.failed"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "upstream_timeout"


def test_processor_publishes_pending_event_and_marks_it_published(
    client,
    auth_headers,
    database,
):
    client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )
    publisher = FakePublisher()
    processor = OutboxProcessor(database.sessions, publisher, 100, 60)

    result = asyncio.run(processor.publish_batch())
    event = load_single_event(database)

    assert result.claimed == 1
    assert result.published == 1
    assert result.failed == 0
    assert len(publisher.events) == 1
    assert event.status == "published"
    assert event.attempts == 1
    assert event.published_at is not None
    assert event.last_error is None


def test_publish_failure_keeps_event_for_retry(
    client,
    auth_headers,
    database,
):
    client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )
    publisher = FakePublisher(error=RuntimeError("rabbit unavailable"))
    processor = OutboxProcessor(database.sessions, publisher, 100, 60)
    before = datetime.now(timezone.utc)

    first = asyncio.run(processor.publish_batch())
    second = asyncio.run(processor.publish_batch())
    event = load_single_event(database)

    available_at = event.available_at
    if available_at.tzinfo is None:
        available_at = available_at.replace(tzinfo=timezone.utc)
    assert first.failed == 1
    assert second.claimed == 0
    assert event.status == "pending"
    assert event.attempts == 1
    assert event.last_error == "rabbit unavailable"
    assert available_at > before


def test_stale_publishing_event_is_reclaimed(
    client,
    auth_headers,
    database,
):
    client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )

    async def make_stale():
        async with database.sessions() as session:
            event = (await session.execute(select(OutboxEvent))).scalar_one()
            event.status = "publishing"
            event.locked_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            await session.commit()

    asyncio.run(make_stale())
    publisher = FakePublisher()
    processor = OutboxProcessor(
        database.sessions,
        publisher,
        batch_size=100,
        max_retry_seconds=60,
        lock_timeout_seconds=60,
    )

    result = asyncio.run(processor.publish_batch())
    event = load_single_event(database)

    assert result.published == 1
    assert event.status == "published"
    assert event.attempts == 1


def test_published_event_is_not_published_twice(
    client,
    auth_headers,
    database,
):
    client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"messages": [{"role": "user", "content": "Hi"}]},
    )
    publisher = FakePublisher()
    processor = OutboxProcessor(database.sessions, publisher, 100, 60)

    first = asyncio.run(processor.publish_batch())
    second = asyncio.run(processor.publish_batch())

    assert first.published == 1
    assert second.claimed == 0
    assert len(publisher.events) == 1
