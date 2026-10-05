"""模型协议返回完整 assistant 消息，保留多轮工具调用所需的 provider 字段。"""

from typing import Any, Protocol


class ModelProvider(Protocol):
    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict[str, Any]: ...
