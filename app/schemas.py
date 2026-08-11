from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Any

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage] = Field(min_length=1)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    stream: bool = False

    model_config = ConfigDict(extra="allow")

    def provider_payload(self, default_model: str) -> Dict[str, Any]:
        payload = self.model_dump(exclude_none=True)
        payload["model"] = self.model or default_model
        return payload


class ErrorDetail(BaseModel):
    message: str
    type: str
    code: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
