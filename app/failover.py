from app.exceptions import (
    GatewayError,
    UpstreamConnectionError,
    UpstreamResponseError,
    UpstreamTimeoutError,
)


RETRYABLE_UPSTREAM_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def should_failover(error: GatewayError) -> bool:
    if isinstance(error, (UpstreamTimeoutError, UpstreamConnectionError)):
        return True
    if isinstance(error, UpstreamResponseError):
        return error.upstream_status_code in RETRYABLE_UPSTREAM_STATUS_CODES
    return False
