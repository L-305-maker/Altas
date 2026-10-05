"""DeepSeek Chat Completions 适配器；真实调用只在显式选择 provider 后发生。

使用 httpx 而非绑定某一 Agent SDK，让应用层无需知道模型供应商。
网络超时与步骤重试分开：这里不做隐藏重试，统一交给持久执行器计数。
"""

from urllib.parse import urlparse

import httpx


class ModelError(Exception):
    pass


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
                response.raise_for_status()
                message = response.json()["choices"][0]["message"]
                if message.get("role") != "assistant":
                    raise ValueError("invalid assistant message")
                # reasoning_content 等字段需回传时，保留原始消息结构而非仅取 content。
                return message
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            # 不将请求对象或供应商响应正文放进异常，避免凭据/原文被日志捕获。
            raise ModelError(
                "model request failed or returned an invalid response"
            ) from None
