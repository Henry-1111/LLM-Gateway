import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol

import aio_pika
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EventAuditLog, InboxEvent
from app.metrics import CONSUMER_DELIVERIES, CONSUMER_RETRIES, DEAD_LETTER_MESSAGES


logger = logging.getLogger("llm_gateway.consumer")


class InvalidEventError(ValueError):
    pass


class EventConflictError(ValueError):
    pass


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    payload: Dict[str, Any]
    raw_body: bytes
    payload_hash: str

    @classmethod
    def parse(cls, body: bytes) -> "EventEnvelope":
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InvalidEventError("Message body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise InvalidEventError("Message body must be a JSON object")
        event_id = payload.get("event_id")
        event_type = payload.get("event_type")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidEventError("event_id is required")
        if not isinstance(event_type, str) or not event_type:
            raise InvalidEventError("event_type is required")
        return cls(
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            raw_body=body,
            payload_hash=hashlib.sha256(body).hexdigest(),
        )


class TransactionalEventHandler(Protocol):
    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None: ...


class AuditEventHandler:
    async def handle(self, session: AsyncSession, event: EventEnvelope) -> None:
        total_tokens = event.payload.get("total_tokens")
        if not isinstance(total_tokens, int) or total_tokens < 0:
            total_tokens = 0
        session.add(
            EventAuditLog(
                event_id=event.event_id,
                event_type=event.event_type,
                request_id=self._optional_string(event.payload.get("request_id")),
                tenant_id=self._optional_string(event.payload.get("tenant_id")),
                total_tokens=total_tokens,
                payload=event.raw_body.decode("utf-8"),
            )
        )

    @staticmethod
    def _optional_string(value: Any) -> Optional[str]:
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class EventProcessResult:
    processed: bool
    duplicate: bool


class InboxEventProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker,
        handler: TransactionalEventHandler,
    ) -> None:
        self._sessions = sessions
        self._handler = handler

    async def process(self, event: EventEnvelope) -> EventProcessResult:
        async with self._sessions() as session:
            existing = await session.get(InboxEvent, event.event_id)
            if existing is not None:
                return self._duplicate_result(existing, event)

            inbox = InboxEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                payload_hash=event.payload_hash,
                status="processing",
            )
            session.add(inbox)
            try:
                await self._handler.handle(session, event)
                inbox.status = "completed"
                inbox.processed_at = datetime.now(timezone.utc)
                await session.commit()
                return EventProcessResult(processed=True, duplicate=False)
            except IntegrityError:
                await session.rollback()
                existing = await session.get(InboxEvent, event.event_id)
                if existing is None:
                    raise
                return self._duplicate_result(existing, event)
            except Exception:
                await session.rollback()
                raise

    @staticmethod
    def _duplicate_result(
        existing: InboxEvent,
        event: EventEnvelope,
    ) -> EventProcessResult:
        if existing.payload_hash != event.payload_hash:
            raise EventConflictError(
                "The same event_id was received with a different payload"
            )
        return EventProcessResult(processed=False, duplicate=True)


class RabbitMQEventConsumer:
    def __init__(
        self,
        url: str,
        exchange_name: str,
        queue_name: str,
        retry_delay_ms: int,
        max_retries: int,
        processor: InboxEventProcessor,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._queue_name = queue_name
        self._retry_delay_ms = retry_delay_ms
        self._max_retries = max_retries
        self._processor = processor
        self._connection = None
        self._channel = None
        self._retry_exchange = None
        self._dead_exchange = None

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.start()
                await stop_event.wait()
            except Exception:
                logger.exception("RabbitMQ consumer connection failed; retrying")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            finally:
                await self.close()

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.set_qos(prefetch_count=20)

        event_exchange = await self._channel.declare_exchange(
            self._exchange_name,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self._retry_exchange = await self._channel.declare_exchange(
            f"{self._exchange_name}.retry",
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        self._dead_exchange = await self._channel.declare_exchange(
            f"{self._exchange_name}.dead",
            aio_pika.ExchangeType.TOPIC,
            durable=True,
        )

        main_queue = await self._channel.declare_queue(self._queue_name, durable=True)
        await main_queue.bind(event_exchange, routing_key="request.*")

        retry_queue = await self._channel.declare_queue(
            f"{self._queue_name}.retry",
            durable=True,
            arguments={
                "x-message-ttl": self._retry_delay_ms,
                "x-dead-letter-exchange": self._exchange_name,
            },
        )
        await retry_queue.bind(self._retry_exchange, routing_key="#")

        dead_queue = await self._channel.declare_queue(
            f"{self._queue_name}.dead",
            durable=True,
        )
        await dead_queue.bind(self._dead_exchange, routing_key="#")
        await main_queue.consume(self._on_message)

    async def close(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process(requeue=True):
            await self.handle_delivery(message)

    async def handle_delivery(self, message) -> None:
        retry_count = self._retry_count(message)
        event_type_label = message.type or "unknown"
        try:
            event = EventEnvelope.parse(message.body)
            if message.message_id and message.message_id != event.event_id:
                raise EventConflictError(
                    "AMQP message_id does not match payload event_id"
                )
            if message.type and message.type != event.event_type:
                raise EventConflictError(
                    "AMQP message type does not match payload event_type"
                )
            result = await self._processor.process(event)
            CONSUMER_DELIVERIES.labels(
                event.event_type,
                "duplicate" if result.duplicate else "processed",
            ).inc()
        except (InvalidEventError, EventConflictError) as exc:
            reason = (
                "invalid_event"
                if isinstance(exc, InvalidEventError)
                else "event_conflict"
            )
            DEAD_LETTER_MESSAGES.labels(event_type_label, reason).inc()
            CONSUMER_DELIVERIES.labels(event_type_label, "dead_lettered").inc()
            await self._publish_copy(
                self._dead_exchange,
                message,
                retry_count,
                str(exc),
            )
        except Exception as exc:
            if retry_count < self._max_retries:
                CONSUMER_RETRIES.labels(event_type_label).inc()
                CONSUMER_DELIVERIES.labels(event_type_label, "retrying").inc()
                await self._publish_copy(
                    self._retry_exchange,
                    message,
                    retry_count + 1,
                    str(exc),
                )
            else:
                DEAD_LETTER_MESSAGES.labels(
                    event_type_label,
                    "retries_exhausted",
                ).inc()
                CONSUMER_DELIVERIES.labels(event_type_label, "dead_lettered").inc()
                await self._publish_copy(
                    self._dead_exchange,
                    message,
                    retry_count,
                    str(exc),
                )

    async def _publish_copy(
        self,
        exchange,
        original,
        retry_count: int,
        error: str,
    ) -> None:
        headers = dict(original.headers or {})
        headers["x-retry-count"] = retry_count
        headers["x-last-error"] = error[:1000]
        message = aio_pika.Message(
            body=original.body,
            message_id=original.message_id,
            type=original.type,
            content_type=original.content_type or "application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers=headers,
        )
        await exchange.publish(
            message,
            routing_key=original.routing_key or original.type or "unknown",
            mandatory=True,
        )

    @staticmethod
    def _retry_count(message) -> int:
        raw_value = (message.headers or {}).get("x-retry-count", 0)
        try:
            return max(int(raw_value), 0)
        except (TypeError, ValueError):
            return 0
