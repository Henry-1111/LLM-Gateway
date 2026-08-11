import asyncio
import json

import pytest
from sqlalchemy import func, select

from app.consumer import (
    AuditEventHandler,
    EventConflictError,
    EventEnvelope,
    InboxEventProcessor,
    RabbitMQEventConsumer,
)
from app.models import EventAuditLog, InboxEvent


def event_body(event_id="event-1", total_tokens=12):
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": "request.completed",
            "request_id": "request-1",
            "tenant_id": "tenant-1",
            "total_tokens": total_tokens,
        },
        separators=(",", ":"),
    ).encode()


class FailingHandler:
    async def handle(self, session, event):
        raise RuntimeError("temporary database failure")


class FakeExchange:
    def __init__(self):
        self.messages = []

    async def publish(self, message, routing_key, mandatory):
        self.messages.append((message, routing_key, mandatory))


class FakeMessage:
    def __init__(self, body, retry_count=0):
        self.body = body
        self.message_id = "event-1"
        self.type = "request.completed"
        self.content_type = "application/json"
        self.routing_key = "request.completed"
        self.headers = {"x-retry-count": retry_count} if retry_count else {}


def count_rows(database):
    async def count():
        async with database.sessions() as session:
            inbox = await session.scalar(select(func.count(InboxEvent.event_id)))
            audit = await session.scalar(select(func.count(EventAuditLog.event_id)))
            return inbox, audit

    return asyncio.run(count())


def test_inbox_and_business_effect_commit_in_one_transaction(database):
    processor = InboxEventProcessor(database.sessions, AuditEventHandler())
    event = EventEnvelope.parse(event_body())

    result = asyncio.run(processor.process(event))

    assert result.processed is True
    assert result.duplicate is False
    assert count_rows(database) == (1, 1)


def test_duplicate_event_has_business_effect_once(database):
    processor = InboxEventProcessor(database.sessions, AuditEventHandler())
    event = EventEnvelope.parse(event_body())

    first = asyncio.run(processor.process(event))
    duplicate = asyncio.run(processor.process(event))

    assert first.processed is True
    assert duplicate.duplicate is True
    assert count_rows(database) == (1, 1)


def test_same_event_id_with_different_payload_is_rejected(database):
    processor = InboxEventProcessor(database.sessions, AuditEventHandler())
    asyncio.run(processor.process(EventEnvelope.parse(event_body(total_tokens=12))))

    with pytest.raises(EventConflictError):
        asyncio.run(
            processor.process(EventEnvelope.parse(event_body(total_tokens=99)))
        )

    assert count_rows(database) == (1, 1)


def test_handler_failure_rolls_back_inbox_and_business_effect(database):
    processor = InboxEventProcessor(database.sessions, FailingHandler())

    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(processor.process(EventEnvelope.parse(event_body())))

    assert count_rows(database) == (0, 0)


def make_consumer(database, handler, max_retries=3):
    processor = InboxEventProcessor(database.sessions, handler)
    consumer = RabbitMQEventConsumer(
        url="amqp://unused",
        exchange_name="events",
        queue_name="audit",
        retry_delay_ms=1000,
        max_retries=max_retries,
        processor=processor,
    )
    consumer._retry_exchange = FakeExchange()
    consumer._dead_exchange = FakeExchange()
    return consumer


def test_temporary_failure_is_published_to_retry_queue(database):
    consumer = make_consumer(database, FailingHandler(), max_retries=3)
    message = FakeMessage(event_body(), retry_count=0)

    asyncio.run(consumer.handle_delivery(message))

    assert len(consumer._retry_exchange.messages) == 1
    assert len(consumer._dead_exchange.messages) == 0
    retried = consumer._retry_exchange.messages[0][0]
    assert retried.headers["x-retry-count"] == 1
    assert "temporary database failure" in retried.headers["x-last-error"]
    assert count_rows(database) == (0, 0)


def test_message_moves_to_dead_letter_after_retry_limit(database):
    consumer = make_consumer(database, FailingHandler(), max_retries=3)
    message = FakeMessage(event_body(), retry_count=3)

    asyncio.run(consumer.handle_delivery(message))

    assert len(consumer._retry_exchange.messages) == 0
    assert len(consumer._dead_exchange.messages) == 1
    dead = consumer._dead_exchange.messages[0][0]
    assert dead.headers["x-retry-count"] == 3


def test_invalid_json_moves_directly_to_dead_letter(database):
    consumer = make_consumer(database, AuditEventHandler())
    message = FakeMessage(b"not-json")

    asyncio.run(consumer.handle_delivery(message))

    assert len(consumer._retry_exchange.messages) == 0
    assert len(consumer._dead_exchange.messages) == 1
    assert "valid JSON" in consumer._dead_exchange.messages[0][0].headers[
        "x-last-error"
    ]


def test_message_id_mismatch_moves_directly_to_dead_letter(database):
    consumer = make_consumer(database, AuditEventHandler())
    message = FakeMessage(event_body(event_id="different-event"))

    asyncio.run(consumer.handle_delivery(message))

    assert len(consumer._dead_exchange.messages) == 1
    assert "message_id" in consumer._dead_exchange.messages[0][0].headers[
        "x-last-error"
    ]


def test_successful_and_duplicate_messages_are_not_republished(database):
    consumer = make_consumer(database, AuditEventHandler())
    message = FakeMessage(event_body())

    asyncio.run(consumer.handle_delivery(message))
    asyncio.run(consumer.handle_delivery(message))

    assert len(consumer._retry_exchange.messages) == 0
    assert len(consumer._dead_exchange.messages) == 0
    assert count_rows(database) == (1, 1)
