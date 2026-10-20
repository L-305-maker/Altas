"""DeepSeek Chat Completions 适配器；真实调用只在显式选择 provider 后发生。

使用 httpx 而非绑定某一 Agent SDK，让应用层无需知道模型供应商。
网络超时与步骤重试分开：这里不做隐藏重试，统一交给持久执行器计数。
"""

from urllib.parse import urlparse

import httpx

from agentflow.application.ports.model_provider import (
    ModelConfigurationError,
    ModelError,
    ModelOutcomeUnknown,
    ModelProtocolError,
    ModelRateLimitedError,
    ModelResponse,
    ModelRetryableError,
)
from agentflow.application.ports.model_request import (
    ModelRequestError,
    ModelRequestOptions,
    PreparedModelRequest,
    ProviderCapabilities,
    TokenCount,
    build_request,
    check_capabilities,
    check_token_window,
    request_digest,
    serialize_request,
)
from agentflow.application.ports.model_usage import DEEPSEEK_USAGE_MAPPING

DEFAULT_MODEL = "deepseek-flash"
SUPPORTED_MODELS = (DEFAULT_MODEL, "deepseek-v4-pro")
LEGACY_MODELS = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
)


def model_lifecycle(model):
    """Explicit allowlist checked against official docs on 2026-10-20."""
    if model in SUPPORTED_MODELS:
        return "supported"
    return "legacy" if model in LEGACY_MODELS else "unsupported"


def capabilities_for(model, provider_key):
    lifecycle = model_lifecycle(model)
    if lifecycle != "supported":
        raise ModelRequestError(
            "ModelDeprecated" if lifecycle == "legacy" else "ModelCapabilityUnsupported"
        )
    # Preserve v1 wire identity for canonical names; no historical rewrite.
    return ProviderCapabilities(
        provider_key=provider_key,
        model=model,
        capability_version="deepseek.adapter.1",
        supports_tools=True,
        supports_json_output=True,
        supported_parameters=("temperature", "top_p"),
        max_output_tokens=4096,
    )


def chat_endpoint(base_url):
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ModelRequestError("ModelEndpointUnsupported")
    return base_url.rstrip("/") + "/chat/completions"


class DeepSeekProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.deepseek.com",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        provider_key="deepseek",
        request_options: ModelRequestOptions | None = None,
    ):
        if not api_key or not model:
            raise ValueError("DeepSeek API key and model are required")
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "model base URL must use HTTPS without embedded credentials"
            )
        self._key, self.model, self.base_url, self._transport = (
            api_key,
            model,
            base_url.rstrip("/"),
            transport,
        )
        self.provider_key = provider_key
        self.request_options = request_options or ModelRequestOptions()

    def prepare_request(self, messages, tools, projection):
        model, provider_key, base_url, options = (
            self.model,
            self.provider_key,
            self.base_url,
            self.request_options,
        )
        key, transport = self._key, self._transport
        capabilities = capabilities_for(model, provider_key)
        endpoint = chat_endpoint(base_url)
        request = build_request(
            messages,
            tools,
            projection,
            capabilities=capabilities,
            options=options,
            adapter_version="deepseek.chat.v1",
            endpoint_fingerprint=request_digest(endpoint),
        )
        send, response = DeepSeekProvider._send, DeepSeekProvider._response

        async def execute():
            data = await send(request.payload(), endpoint, key, transport)
            return response(data)

        return PreparedModelRequest(request, execute)

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        data = await self._request(messages, tools)
        try:
            message = data["choices"][0]["message"]
            if message.get("role") != "assistant":
                raise ValueError
            return message
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelProtocolError() from None

    async def respond(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> ModelResponse:
        data = await self._request(messages, tools)
        return self._response(data)

    @staticmethod
    def _response(data):
        try:
            choice = data["choices"][0]
            return ModelResponse(
                assistant_message=choice["message"],
                finish_reason=choice["finish_reason"],
                usage=data.get("usage"),
                normalized_usage=DEEPSEEK_USAGE_MAPPING.normalize(data.get("usage")),
                model=data.get("model"),
                request_id=data.get("id"),
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelProtocolError() from None

    async def _request(self, messages, tools):
        model, options = self.model, self.request_options
        key, transport = self._key, self._transport
        try:
            capabilities = capabilities_for(model, self.provider_key)
            endpoint = chat_endpoint(self.base_url)
            check_capabilities(capabilities, options, tools, messages)
            check_token_window(capabilities, options, TokenCount())
            context = {"messages": messages, **({"tools": tools} if tools else {})}
            payload = serialize_request(context, model, options)
        except ModelRequestError:
            raise ModelConfigurationError() from None
        return await self._send(payload, endpoint, key, transport)

    @staticmethod
    async def _send(payload, endpoint, key, transport):
        try:
            async with httpx.AsyncClient(
                timeout=45, transport=transport, follow_redirects=False
            ) as client:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
                if response.status_code == 429:
                    try:
                        hint = float(response.headers.get("Retry-After", ""))
                    except ValueError:
                        hint = None
                    raise ModelRateLimitedError(retry_after_seconds=hint)
                if 400 <= response.status_code < 500:
                    raise ModelConfigurationError()
                if not response.is_success:
                    raise ModelOutcomeUnknown()
                return response.json()
        except ModelError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise ModelRetryableError() from None
        except httpx.HTTPError:
            raise ModelOutcomeUnknown() from None
        except ValueError:
            raise ModelProtocolError() from None
