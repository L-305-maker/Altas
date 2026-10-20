"""确定性的离线替身，仅验证接口和业务流程，不模拟真实推理质量。"""

from copy import deepcopy

from agentflow.application.ports.model_provider import ModelResponse
from agentflow.application.ports.model_request import (
    ModelRequestError,
    ModelRequestOptions,
    PreparedModelRequest,
    ProviderCapabilities,
    build_request,
)


class MockProvider:
    def __init__(
        self,
        responses: list[dict | ModelResponse | Exception] | None = None,
        *,
        provider_key="mock",
        model="mock-v1",
        request_options=None,
        capabilities=None,
        token_counter=None,
    ):
        self.responses = [
            r if isinstance(r, Exception) else deepcopy(r)
            for r in (responses or [{"role": "assistant", "content": "离线测试输出"}])
        ]
        self.calls = 0
        self.contexts: list[list[dict]] = []
        self.provider_key, self.model = provider_key, model
        self.request_options = request_options or ModelRequestOptions()
        self.capabilities = capabilities
        self.token_counter = token_counter
        self.requests = []

    def prepare_request(self, messages, tools, projection):
        capabilities = self.capabilities or ProviderCapabilities(
            provider_key=self.provider_key,
            model=self.model,
            capability_version="1",
            supports_tools=True,
            supports_json_output=True,
            max_output_tokens=65536,
            supported_parameters=("temperature", "top_p"),
        )
        if (capabilities.provider_key, capabilities.model) != (
            self.provider_key,
            self.model,
        ):
            raise ModelRequestError("ModelCapabilityUnsupported")
        request = build_request(
            messages,
            tools,
            projection,
            capabilities=capabilities,
            options=self.request_options,
            adapter_version="mock.v1",
            token_counter=self.token_counter,
        )
        respond = self.respond

        async def execute():
            payload = request.payload()
            self.requests.append(request.contract)
            return await respond(payload["messages"], payload.get("tools", []))

        return PreparedModelRequest(request, execute)

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
