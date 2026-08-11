from typing import Dict, Optional


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        code: str,
        status_code: int,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.headers = headers or {}


class UnauthorizedError(GatewayError):
    def __init__(self) -> None:
        super().__init__("Invalid or missing gateway API key.", "invalid_api_key", 401)


class StreamingNotSupportedError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "Streaming is not supported by this MVP.",
            "streaming_not_supported",
            400,
        )


class ModelNotAvailableError(GatewayError):
    def __init__(self, model: str) -> None:
        super().__init__(
            f"No enabled provider supports model '{model}'.",
            "model_not_available",
            404,
        )


class InvalidIdempotencyKeyError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "Idempotency-Key must contain between 8 and 255 visible characters.",
            "invalid_idempotency_key",
            400,
        )


class IdempotencyConflictError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "This Idempotency-Key was already used with a different request.",
            "idempotency_conflict",
            409,
        )


class IdempotencyInProgressError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "A request with this Idempotency-Key is already being processed.",
            "idempotency_in_progress",
            409,
        )


class RateLimitExceededError(GatewayError):
    def __init__(
        self,
        limit: int,
        remaining: int,
        reset_at: int,
        retry_after: int,
    ) -> None:
        super().__init__(
            "Rate limit exceeded.",
            "rate_limit_exceeded",
            429,
            headers={
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": str(retry_after),
            },
        )


class RateLimitUnavailableError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "The rate limit service is unavailable.",
            "rate_limit_unavailable",
            503,
        )


class QuotaExceededError(GatewayError):
    def __init__(self, limit: int, remaining: int, required: int, period: str) -> None:
        super().__init__(
            "Insufficient token quota for this request.",
            "quota_exceeded",
            429,
            headers={
                "X-Quota-Limit": str(limit),
                "X-Quota-Remaining": str(remaining),
                "X-Quota-Required": str(required),
                "X-Quota-Period": period,
            },
        )


class QuotaUnavailableError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "The quota service is unavailable.",
            "quota_unavailable",
            503,
        )


class UpstreamTimeoutError(GatewayError):
    def __init__(self) -> None:
        super().__init__("The model provider timed out.", "upstream_timeout", 504)


class UpstreamConnectionError(GatewayError):
    def __init__(self) -> None:
        super().__init__(
            "The model provider could not be reached.",
            "upstream_connection_error",
            502,
        )


class UpstreamResponseError(GatewayError):
    def __init__(self, status_code: int, detail: Optional[str] = None) -> None:
        public_status = 429 if status_code == 429 else 502
        message = detail or "The model provider returned an error."
        super().__init__(message, "upstream_error", public_status)
        self.upstream_status_code = status_code
