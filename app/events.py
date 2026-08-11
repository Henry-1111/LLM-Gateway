from datetime import datetime, timezone
from typing import Any, Dict

from app.models import RequestLog


def request_event_payload(
    request_log: RequestLog,
    event_id: str,
    event_type: str,
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "request_id": request_log.id,
        "tenant_id": request_log.tenant_id,
        "api_key_id": request_log.api_key_id,
        "provider_name": request_log.provider_name,
        "model": request_log.model,
        "status": request_log.status,
        "status_code": request_log.status_code,
        "prompt_tokens": request_log.prompt_tokens,
        "completion_tokens": request_log.completion_tokens,
        "total_tokens": request_log.total_tokens,
        "duration_ms": request_log.duration_ms,
        "error_code": request_log.error_code,
    }
