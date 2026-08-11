import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol, Sequence

import aio_pika
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models import OutboxEvent
from app.metrics import OUTBOX_PUBLISHED, OUTBOX_PUBLISH_FAILURES


logger = logging.getLogger("llm_gateway.outbox")


class EventPublisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None: ...

    async def close(self) -> None: ...


class RabbitMQPublisher:
    def __init__(self, url: str, exchange_name: str) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._connection = None
        self._channel = None
        self._exchange = None
        self._connect_lock = asyncio.Lock()

    async def publish(self, event: OutboxEvent) -> None:
        exchange = await self._get_exchange()
        message = aio_pika.Message(
            body=event.payload.encode("utf-8"),
            message_id=event.id,
            type=event.event_type,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            timestamp=datetime.now(timezone.utc),
            headers={"aggregate_id": event.aggregate_id},
        )
        await exchange.publish(
            message,
            routing_key=event.event_type,
            mandatory=True,
        )

    async def close(self) -> None:
        if self._connection is not None and not self._connection.is_closed:
            await self._connection.close()

    async def _get_exchange(self):
        if self._exchange is not None and not self._connection.is_closed:
            return self._exchange
        async with self._connect_lock:
            if self._exchange is not None and not self._connection.is_closed:
                return self._exchange
            self._connection = await aio_pika.connect_robust(self._url)
            self._channel = await self._connection.channel(publisher_confirms=True)
            self._exchange = await self._channel.declare_exchange(
                self._exchange_name,
                aio_pika.ExchangeType.TOPIC,
                durable=True,
            )
            return self._exchange


@dataclass(frozen=True)
class PublishBatchResult:
    claimed: int
    published: int
    failed: int


class OutboxProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker,
        publisher: EventPublisher,
        batch_size: int,
        max_retry_seconds: int,
        lock_timeout_seconds: int = 300,
    ) -> None:
        self._sessions = sessions
        self._publisher = publisher
        self._batch_size = batch_size
        self._max_retry_seconds = max_retry_seconds
        self._lock_timeout_seconds = lock_timeout_seconds

    async def publish_batch(self) -> PublishBatchResult:
        events = await self._claim_batch()
        published = 0
        failed = 0
        for event in events:
            try:
                await self._publisher.publish(event)
            except Exception as exc:
                failed += 1
                OUTBOX_PUBLISH_FAILURES.labels(event.event_type).inc()
                await self._mark_failed(event, exc)
                logger.warning(
                    "Outbox publish failed event_id=%s event_type=%s attempt=%s",
                    event.id,
                    event.event_type,
                    event.attempts,
                    exc_info=True,
                )
            else:
                published += 1
                await self._mark_published(event.id)
                OUTBOX_PUBLISHED.labels(event.event_type).inc()
        return PublishBatchResult(len(events), published, failed)

    async def run(self, stop_event: asyncio.Event, poll_interval_seconds: float) -> None:
        while not stop_event.is_set():
            try:
                result = await self.publish_batch()
            except Exception:
                logger.exception("Unexpected outbox processor failure")
                result = PublishBatchResult(0, 0, 0)
            if result.claimed == 0:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass

    async def _claim_batch(self) -> Sequence[OutboxEvent]:
        now = datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=self._lock_timeout_seconds)
        async with self._sessions() as session:
            result = await session.execute(
                select(OutboxEvent)
                .where(
                    or_(
                        and_(
                            OutboxEvent.status == "pending",
                            OutboxEvent.available_at <= now,
                        ),
                        and_(
                            OutboxEvent.status == "publishing",
                            OutboxEvent.locked_at <= stale_before,
                        ),
                    )
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            events = list(result.scalars())
            for event in events:
                event.status = "publishing"
                event.locked_at = now
                event.attempts += 1
            await session.commit()
            return events

    async def _mark_published(self, event_id: str) -> None:
        async with self._sessions() as session:
            event = await session.get(OutboxEvent, event_id)
            if event is None:
                raise RuntimeError(f"Outbox event {event_id} does not exist")
            event.status = "published"
            event.published_at = datetime.now(timezone.utc)
            event.locked_at = None
            event.last_error = None
            await session.commit()

    async def _mark_failed(self, event: OutboxEvent, error: Exception) -> None:
        retry_seconds = min(2 ** max(event.attempts - 1, 0), self._max_retry_seconds)
        async with self._sessions() as session:
            stored = await session.get(OutboxEvent, event.id)
            if stored is None:
                raise RuntimeError(f"Outbox event {event.id} does not exist")
            stored.status = "pending"
            stored.available_at = datetime.now(timezone.utc) + timedelta(
                seconds=retry_seconds
            )
            stored.locked_at = None
            stored.last_error = str(error)[:2000]
            await session.commit()
