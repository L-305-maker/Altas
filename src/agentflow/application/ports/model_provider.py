"""模型协议返回完整 assistant 消息，保留多轮工具调用所需的 provider 字段。"""

import json
import math
from typing import Any, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from agentflow.application.ports.model_usage import MAX_USAGE_BYTES, ModelUsage


class ModelError(Exception):
    code = "ModelError"

    def __init__(self):
        super().__init__(self.code)


class ModelRetryableError(ModelError):
    """供应商确认未完成的请求，允许有界重试。"""

    code = "ModelRetryable"

    def __init__(self, *, retry_after_seconds: float | None = None):
        super().__init__()
        self.retry_after_seconds = (
            retry_after_seconds
            if type(retry_after_seconds) in {int, float}
            and math.isfinite(retry_after_seconds)
            and retry_after_seconds >= 0
            else None
        )


class ModelOutcomeUnknown(ModelError):
    code = "ModelOutcomeUnknown"


class ModelProtocolError(ModelError):
    code = "ModelProtocolError"


class ModelConfigurationError(ModelError):
    code = "ModelConfigurationError"


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=80)
    arguments: str


class ModelToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=128)
    type: Literal["function"] = "function"
    function: FunctionCall


class AssistantMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    role: Literal["assistant"]
    content: str | None = None
    tool_calls: tuple[ModelToolCall, ...] = ()
    reasoning_content: str | None = None

    @field_validator("tool_calls", mode="before")
    @classmethod
    def nullable_calls(cls, value):
        return () if value is None else value

    @model_validator(mode="after")
    def unique_calls(self) -> Self:
        if len(self.tool_calls) > 16 or len({c.id for c in self.tool_calls}) != len(
            self.tool_calls
        ):
            raise ValueError("invalid tool call batch")
        return self


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    assistant_message: AssistantMessage
    finish_reason: str = Field(min_length=1)
    usage: dict[str, JsonValue] | None = None
    normalized_usage: ModelUsage | None = None
    model: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def finite_usage(self) -> Self:
        encoded = json.dumps(self.usage, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_USAGE_BYTES:
            raise ValueError("provider usage exceeds storage limit")
        if self.usage is None and self.normalized_usage not in {None, ModelUsage()}:
            raise ValueError("normalized usage requires raw provider measurements")
        return self


class DurableModelProvider(Protocol):
    async def respond(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ModelResponse: ...


class ModelProvider(Protocol):
    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict[str, Any]: ...
