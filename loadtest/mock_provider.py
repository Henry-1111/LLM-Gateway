import asyncio
import os
import time
import uuid
from typing import Any, Dict

from fastapi import FastAPI


app = FastAPI(title="LLM Gateway load-test provider")


def _delay_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("MOCK_PROVIDER_DELAY_MS", "20")) / 1000)
    except ValueError:
        return 0.02


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(payload: Dict[str, Any]) -> Dict[str, Any]:
    await asyncio.sleep(_delay_seconds())
    messages = payload.get("messages") or []
    prompt_tokens = max(1, sum(len(str(item.get("content", ""))) for item in messages) // 4)
    completion_tokens = 8
    return {
        "id": f"chatcmpl-load-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", "load-test-model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Load test response"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
