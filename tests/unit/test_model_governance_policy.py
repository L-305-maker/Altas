import json

import httpx
import pytest
from pydantic import ValidationError

from agentflow.application.agents.durable_runner import DurableAgentConfig
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.infrastructure.models.deepseek import DeepSeekProvider


@pytest.mark.parametrize(
    "changes",
    [
        {"max_context_bytes": 0},
        {"max_context_bytes": True},
        {"retry_base_seconds": 0},
        {"retry_base_seconds": -1},
        {"retry_max_seconds": float("nan")},
        {"retry_base_seconds": float("inf")},
        {"retry_base_seconds": 5, "retry_max_seconds": 4},
        {"retry_max_seconds": 3601},
    ],
)
def test_invalid_execution_limits_rejected(changes):
    with pytest.raises(ValidationError):
        DurableAgentConfig(**changes)


def test_retry_policy_is_bounded_and_uses_confirmed_attempt_count():
    config = DurableAgentConfig(retry_base_seconds=2, retry_max_seconds=5)
    assert [config.retry_delay(i) for i in (1, 2, 3, 10**9)] == [2, 4, 5, 5]
    assert config.retry_delay(1, 3) == 3
    assert config.retry_delay(1, 1e100) == 5
    assert config.retry_delay(3, 1) == 5
    assert ModelRetryableError().retry_after_seconds is None
    assert str(ModelRetryableError()) == "ModelRetryable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header,expected",
    [
        ("3", 3),
        ("1.5", 1.5),
        ("9999999", 9999999),
        ("-1", None),
        ("NaN", None),
        ("Infinity", None),
        ("garbage", None),
    ],
)
async def test_retry_after_is_safe_hint_without_hidden_requests(header, expected):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            429, headers={"Retry-After": header}, text="private failure body"
        )

    provider = DeepSeekProvider(
        "test-only-key", "deepseek-flash", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ModelRetryableError) as caught:
        await provider.respond([{"role": "user", "content": "private prompt"}])
    assert caught.value.retry_after_seconds == expected
    assert str(caught.value) == "ModelRetryable"
    assert len(requests) == 1


def test_context_byte_count_includes_tool_schema_and_json_escaping():
    from agentflow.application.agents.context import context_bytes

    messages = [
        {
            "role": "tool",
            "content": json.dumps({"nested": ['中文🧪\n"\\']}, ensure_ascii=False),
            "tool_call_id": "core-id",
        }
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "中文",
                "parameters": {"type": "object"},
            },
        }
    ]
    body = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert context_bytes(messages, tools) == len(body)
    assert context_bytes(messages, []) == len(
        json.dumps(
            {"messages": messages}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )
