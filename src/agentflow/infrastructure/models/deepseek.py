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
from agentflow.application.ports.model_usage import DEEPSEEK_USAGE_MAPPING


class DeepSeekProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.deepseek.com",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
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
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "max_tokens": 4096,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(
                timeout=45, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
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
