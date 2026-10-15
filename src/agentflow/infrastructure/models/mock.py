"""确定性的离线替身，仅验证接口和业务流程，不模拟真实推理质量。"""

from copy import deepcopy

from agentflow.application.ports.model_provider import ModelResponse


class MockProvider:
    def __init__(self, responses: list[dict | ModelResponse | Exception] | None = None):
        self.responses = [
            r if isinstance(r, Exception) else deepcopy(r)
            for r in (responses or [{"role": "assistant", "content": "离线测试输出"}])
        ]
        self.calls = 0
        self.contexts: list[list[dict]] = []

    def _next(self, messages):
        if self.calls >= len(self.responses):
            raise RuntimeError("mock response sequence exhausted")
        response = self.responses[self.calls]
        self.calls += 1
        self.contexts.append(deepcopy(messages))
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        response = self._next(messages)
        if isinstance(response, ModelResponse):
            return response.assistant_message.model_dump(mode="json", exclude_none=True)
        return response

    async def respond(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ModelResponse:
        response = self._next(messages)
        if isinstance(response, ModelResponse):
            return response
        if "assistant_message" in response:
            return ModelResponse.model_validate(response)
        return ModelResponse(
            assistant_message=response,
            finish_reason="tool_calls" if response.get("tool_calls") else "stop",
        )
