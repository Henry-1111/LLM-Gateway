import logging
import uuid
import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional

import httpx
import redis.asyncio as redis_async
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.auth import ApiKeyService, Principal
from app.config import ProviderSettings, Settings, get_settings
from app.consumer import AuditEventHandler, InboxEventProcessor, RabbitMQEventConsumer
from app.database import Database
from app.exceptions import GatewayError, UnauthorizedError
from app.idempotency import (
    IdempotencyReplay,
    IdempotencyReservation,
    IdempotencyService,
    request_fingerprint,
)
from app.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS, IDEMPOTENCY_REPLAYS
from app.outbox import OutboxProcessor, RabbitMQPublisher
from app.provider import OpenAICompatibleProvider
from app.quota import (
    NoopQuotaManager,
    QuotaManagerProtocol,
    QuotaReservation,
    QuotaSettlement,
    RedisQuotaManager,
)
from app.rate_limit import (
    NoopRateLimiter,
    RateLimitDecision,
    RateLimiterProtocol,
    RedisRateLimiter,
)
from app.repositories import ProviderAttemptRepository, RequestLogRepository
from app.routing import ChatProvider, ProviderRoute, ProviderRouter
from app.schemas import ChatCompletionRequest
from app.service import ChatService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(
    settings: Optional[Settings] = None,
    provider: Optional[ChatProvider] = None,
    provider_router: Optional[ProviderRouter] = None,
    database: Optional[Database] = None,
    rate_limiter: Optional[RateLimiterProtocol] = None,
    quota_manager: Optional[QuotaManagerProtocol] = None,
) -> FastAPI:
    if provider is not None and provider_router is not None:
        raise ValueError("Pass either provider or provider_router, not both")
    resolved_settings = settings or get_settings()
    resolved_database = database or Database(resolved_settings.database_url)
    owns_database = database is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resolved_settings.database_auto_create:
            await resolved_database.create_schema()
        client: Optional[httpx.AsyncClient] = None
        redis_client = None
        outbox_publisher = None
        outbox_stop_event = None
        outbox_task = None
        event_consumer = None
        consumer_stop_event = None
        consumer_task = None
        if provider_router is not None:
            resolved_router = provider_router
        elif provider is not None:
            resolved_router = ProviderRouter(
                [
                    ProviderRoute(
                        name="injected-provider",
                        provider=provider,
                        models=("*",),
                        priority=0,
                    )
                ]
            )
        else:
            provider_configs = _provider_configs(resolved_settings)
            client = httpx.AsyncClient()
            resolved_router = ProviderRouter(
                ProviderRoute(
                    name=config.name,
                    provider=OpenAICompatibleProvider(
                        client=client,
                        api_key=config.api_key,
                        base_url=config.base_url,
                        timeout_seconds=config.timeout_seconds,
                    ),
                    models=tuple(config.models),
                    priority=config.priority,
                    enabled=config.enabled,
                )
                for config in provider_configs
            )
        app.state.chat_service = ChatService(
            provider_router=resolved_router,
            request_logs=RequestLogRepository(resolved_database.sessions),
            provider_attempts=ProviderAttemptRepository(resolved_database.sessions),
            default_model=resolved_settings.default_model,
            max_provider_attempts=resolved_settings.max_provider_attempts,
        )
        app.state.api_key_service = ApiKeyService(resolved_database.sessions)
        app.state.idempotency_service = IdempotencyService(
            resolved_database.sessions,
            resolved_settings.idempotency_ttl_seconds,
        )
        needs_redis = (
            resolved_settings.rate_limit_enabled and rate_limiter is None
        ) or (resolved_settings.quota_enabled and quota_manager is None)
        if needs_redis:
            redis_client = redis_async.Redis.from_url(
                resolved_settings.redis_url,
                decode_responses=True,
            )
        app.state.database = resolved_database
        app.state.redis_client = redis_client
        if rate_limiter is not None:
            app.state.rate_limiter = rate_limiter
        elif resolved_settings.rate_limit_enabled:
            app.state.rate_limiter = RedisRateLimiter(
                redis=redis_client,
                limit=resolved_settings.rate_limit_requests,
                window_seconds=resolved_settings.rate_limit_window_seconds,
            )
        else:
            app.state.rate_limiter = NoopRateLimiter()
        if quota_manager is not None:
            app.state.quota_manager = quota_manager
        elif resolved_settings.quota_enabled:
            app.state.quota_manager = RedisQuotaManager(
                redis=redis_client,
                default_limit=resolved_settings.quota_default_tokens,
                reservation_ttl_seconds=resolved_settings.quota_reservation_ttl_seconds,
            )
        else:
            app.state.quota_manager = NoopQuotaManager()
        if resolved_settings.outbox_publisher_enabled:
            outbox_publisher = RabbitMQPublisher(
                resolved_settings.rabbitmq_url,
                resolved_settings.rabbitmq_exchange,
            )
            outbox_processor = OutboxProcessor(
                sessions=resolved_database.sessions,
                publisher=outbox_publisher,
                batch_size=resolved_settings.outbox_batch_size,
                max_retry_seconds=resolved_settings.outbox_max_retry_seconds,
            )
            outbox_stop_event = asyncio.Event()
            outbox_task = asyncio.create_task(
                outbox_processor.run(
                    outbox_stop_event,
                    resolved_settings.outbox_poll_interval_seconds,
                ),
                name="outbox-publisher",
            )
        if resolved_settings.event_consumer_enabled:
            event_consumer = RabbitMQEventConsumer(
                url=resolved_settings.rabbitmq_url,
                exchange_name=resolved_settings.rabbitmq_exchange,
                queue_name=resolved_settings.rabbitmq_consumer_queue,
                retry_delay_ms=resolved_settings.rabbitmq_retry_delay_ms,
                max_retries=resolved_settings.rabbitmq_max_retries,
                processor=InboxEventProcessor(
                    resolved_database.sessions,
                    AuditEventHandler(),
                ),
            )
            consumer_stop_event = asyncio.Event()
            consumer_task = asyncio.create_task(
                event_consumer.run(consumer_stop_event),
                name="rabbitmq-event-consumer",
            )
        yield
        if consumer_stop_event is not None and consumer_task is not None:
            consumer_stop_event.set()
            await consumer_task
        if outbox_stop_event is not None and outbox_task is not None:
            outbox_stop_event.set()
            await outbox_task
        if outbox_publisher is not None:
            await outbox_publisher.close()
        if client is not None:
            await client.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        if owns_database:
            await resolved_database.close()

    app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings

    @app.middleware("http")
    async def prometheus_http_metrics(request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        started_at = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_label = getattr(route, "path", "unmatched")
            method = request.method
            HTTP_REQUESTS.labels(method, route_label, str(status_code)).inc()
            HTTP_REQUEST_DURATION.labels(method, route_label).observe(
                time.monotonic() - started_at
            )

    async def require_api_key(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise UnauthorizedError()
        supplied_key = authorization.removeprefix("Bearer ").strip()
        if not supplied_key:
            raise UnauthorizedError()
        return await request.app.state.api_key_service.authenticate(supplied_key)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_gateway_error_content(exc),
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logging.getLogger("llm_gateway").exception("Unhandled request error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "An internal gateway error occurred.",
                    "type": "gateway_error",
                    "code": "internal_error",
                }
            },
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> Response:
        checks = {}
        try:
            await request.app.state.database.ping()
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = "error"
            logging.getLogger("llm_gateway.readiness").warning(
                "Database readiness check failed",
                exc_info=exc,
            )
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "checks": checks},
            )
        redis_client = request.app.state.redis_client
        if redis_client is not None:
            try:
                await redis_client.ping()
                checks["redis"] = "ok"
            except Exception as exc:
                checks["redis"] = "error"
                logging.getLogger("llm_gateway.readiness").warning(
                    "Redis readiness check failed",
                    exc_info=exc,
                )
                return JSONResponse(
                    status_code=503,
                    content={"status": "not_ready", "checks": checks},
                )
        return JSONResponse(content={"status": "ready", "checks": checks})

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(
            content=generate_latest(),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    async def enforce_rate_limit(
        request: Request,
        principal: Principal = Depends(require_api_key),
    ) -> Optional[RateLimitDecision]:
        return await request.app.state.rate_limiter.check(principal)

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: ChatCompletionRequest,
        raw_request: Request,
        principal: Principal = Depends(require_api_key),
        rate_limit: Optional[RateLimitDecision] = Depends(enforce_rate_limit),
        idempotency_key: Optional[str] = Header(
            default=None,
            alias="Idempotency-Key",
        ),
    ) -> JSONResponse:
        reservation: Optional[IdempotencyReservation] = None
        provider_payload = request.provider_payload(resolved_settings.default_model)
        if idempotency_key is not None:
            fingerprint = request_fingerprint(
                {
                    "endpoint": "/v1/chat/completions",
                    "body": provider_payload,
                }
            )
            decision = await raw_request.app.state.idempotency_service.begin(
                principal.tenant_id,
                idempotency_key,
                fingerprint,
            )
            if isinstance(decision, IdempotencyReplay):
                IDEMPOTENCY_REPLAYS.labels(str(decision.status_code)).inc()
                return JSONResponse(
                    status_code=decision.status_code,
                    content=decision.body,
                    headers={
                        **decision.headers,
                        **_rate_limit_headers(rate_limit),
                        "Idempotency-Replayed": "true",
                    },
                )
            reservation = decision

        request_id = str(uuid.uuid4())
        quota_reservation: Optional[QuotaReservation] = None
        try:
            requested_tokens = RedisQuotaManager.estimate_tokens(
                provider_payload,
                resolved_settings.quota_default_completion_reserve_tokens,
            )
            quota_reservation = await raw_request.app.state.quota_manager.reserve(
                principal,
                request_id,
                requested_tokens,
            )
            if quota_reservation is not None and "max_tokens" not in provider_payload:
                provider_payload["max_tokens"] = (
                    resolved_settings.quota_default_completion_reserve_tokens
                )
            result = await raw_request.app.state.chat_service.create_chat_completion(
                request,
                principal,
                request_id=request_id,
                provider_payload=provider_payload,
            )
        except GatewayError as exc:
            if quota_reservation is not None:
                await _release_quota_safely(
                    raw_request,
                    quota_reservation,
                )
            if reservation is not None:
                await raw_request.app.state.idempotency_service.complete(
                    reservation.record_id,
                    status="failed",
                    status_code=exc.status_code,
                    body=_gateway_error_content(exc),
                )
            raise
        except Exception:
            if quota_reservation is not None:
                await _release_quota_safely(
                    raw_request,
                    quota_reservation,
                )
            if reservation is not None:
                await raw_request.app.state.idempotency_service.complete(
                    reservation.record_id,
                    status="failed",
                    status_code=500,
                    body={
                        "error": {
                            "message": "An internal gateway error occurred.",
                            "type": "gateway_error",
                            "code": "internal_error",
                        }
                    },
                )
            raise

        quota_settlement: Optional[QuotaSettlement] = None
        if quota_reservation is not None:
            try:
                usage = result.response.get("usage") or {}
                quota_settlement = await raw_request.app.state.quota_manager.commit(
                    quota_reservation,
                    usage.get("total_tokens"),
                )
            except GatewayError as exc:
                if reservation is not None:
                    await raw_request.app.state.idempotency_service.complete(
                        reservation.record_id,
                        status="failed",
                        status_code=exc.status_code,
                        body=_gateway_error_content(exc),
                    )
                raise

        stable_response_headers = {
            "X-Request-ID": result.request_id,
            "X-Provider": result.provider_name,
            "X-Provider-Attempts": str(result.provider_attempts),
        }
        response_headers = {
            **stable_response_headers,
            **_rate_limit_headers(rate_limit),
            **_quota_headers(quota_settlement),
        }
        if reservation is not None:
            await raw_request.app.state.idempotency_service.complete(
                reservation.record_id,
                status="completed",
                status_code=200,
                body=result.response,
                headers=stable_response_headers,
                request_id=result.request_id,
            )
            response_headers["Idempotency-Replayed"] = "false"

        return JSONResponse(
            status_code=200,
            content=result.response,
            headers=response_headers,
        )

    return app


def _provider_configs(settings: Settings) -> List[ProviderSettings]:
    if settings.providers:
        return settings.providers
    if not settings.provider_api_key:
        raise RuntimeError("PROVIDERS or PROVIDER_API_KEY must be configured")
    return [
        ProviderSettings(
            name="default",
            api_key=settings.provider_api_key,
            base_url=settings.provider_base_url,
            models=["*"],
            priority=100,
            timeout_seconds=settings.provider_timeout_seconds,
        )
    ]


def _gateway_error_content(exc: GatewayError) -> dict:
    return {
        "error": {
            "message": exc.message,
            "type": "gateway_error",
            "code": exc.code,
        }
    }


def _rate_limit_headers(decision: Optional[RateLimitDecision]) -> dict:
    return decision.headers() if decision is not None else {}


def _quota_headers(settlement: Optional[QuotaSettlement]) -> dict:
    return settlement.headers() if settlement is not None else {}


async def _release_quota_safely(
    request: Request,
    reservation: QuotaReservation,
) -> None:
    try:
        await request.app.state.quota_manager.release(reservation)
    except GatewayError:
        logging.getLogger("llm_gateway.quota").exception(
            "Failed to release quota reservation %s; it will be reclaimed after TTL",
            reservation.reservation_id,
        )
