from prometheus_client import Counter, Histogram


HTTP_REQUESTS = Counter(
    "gateway_http_requests_total",
    "HTTP requests processed by the gateway.",
    ("method", "route", "status_code"),
)
HTTP_REQUEST_DURATION = Histogram(
    "gateway_http_request_duration_seconds",
    "Gateway HTTP request duration in seconds.",
    ("method", "route"),
)
PROVIDER_ATTEMPTS = Counter(
    "gateway_provider_attempts_total",
    "Calls attempted against model providers.",
    ("provider", "outcome", "error_code"),
)
PROVIDER_DURATION = Histogram(
    "gateway_provider_duration_seconds",
    "Individual provider call duration in seconds.",
    ("provider", "outcome"),
)
PROVIDER_FAILOVERS = Counter(
    "gateway_provider_failovers_total",
    "Provider failovers performed by the gateway.",
    ("from_provider", "to_provider", "reason"),
)
TOKENS = Counter(
    "gateway_tokens_total",
    "Tokens reported by successful provider responses.",
    ("provider", "token_type"),
)
RATE_LIMIT_REJECTIONS = Counter(
    "gateway_rate_limit_rejections_total",
    "Requests rejected by Redis rate limiting.",
)
QUOTA_REJECTIONS = Counter(
    "gateway_quota_rejections_total",
    "Requests rejected due to insufficient token quota.",
)
IDEMPOTENCY_REPLAYS = Counter(
    "gateway_idempotency_replays_total",
    "Responses replayed from the idempotency store.",
    ("status_code",),
)
OUTBOX_PUBLISH_FAILURES = Counter(
    "gateway_outbox_publish_failures_total",
    "Outbox events that failed to publish.",
    ("event_type",),
)
OUTBOX_PUBLISHED = Counter(
    "gateway_outbox_published_total",
    "Outbox events confirmed by RabbitMQ.",
    ("event_type",),
)
CONSUMER_DELIVERIES = Counter(
    "gateway_consumer_deliveries_total",
    "RabbitMQ consumer delivery outcomes.",
    ("event_type", "outcome"),
)
CONSUMER_RETRIES = Counter(
    "gateway_consumer_retries_total",
    "Messages copied to the retry queue.",
    ("event_type",),
)
DEAD_LETTER_MESSAGES = Counter(
    "gateway_dead_letter_messages_total",
    "Messages copied to the dead-letter queue.",
    ("event_type", "reason"),
)
