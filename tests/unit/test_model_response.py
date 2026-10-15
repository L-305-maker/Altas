import httpx
import pytest
from pydantic import ValidationError

from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelProtocolError,
    ModelResponse,
    ModelRetryableError,
)
from agentflow.infrastructure.models.deepseek import DeepSeekProvider
from agentflow.infrastructure.models.mock import MockProvider


def response(**changes):
    return ModelResponse(
        **{
            "assistant_message": {"role": "assistant", "content": "done"},
            "finish_reason": "stop",
            **changes,
        }
    )


def test_response_metadata_and_unknown_usage():
    value = response(model="test", request_id="request")
    assert value.usage is None
    assert ModelResponse.model_validate_json(value.model_dump_json()) == value
    assert response(finish_reason="length").finish_reason == "length"


def test_duplicate_provider_ids_rejected():
    call = {
        "id": "a",
        "type": "function",
        "function": {"name": "read", "arguments": "{}"},
    }
    with pytest.raises(ValidationError):
        response(assistant_message={"role": "assistant", "tool_calls": [call, call]})


@pytest.mark.asyncio
async def test_mock_typed_responses_errors_and_legacy():
    provider = MockProvider(
        [response(), ModelRetryableError(), response(finish_reason="length")]
    )
    assert (await provider.respond([])).finish_reason == "stop"
    with pytest.raises(ModelRetryableError):
        await provider.respond([])
    assert (await provider.respond([])).finish_reason == "length"
    assert provider.calls == 3
    assert await MockProvider().complete([]) == {
        "role": "assistant",
        "content": "离线测试输出",
    }


@pytest.mark.asyncio
async def test_deepseek_metadata_and_no_hidden_retry():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "req",
                "model": "model",
                "usage": {"total_tokens": 12},
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    provider = DeepSeekProvider(
        "test-key", "model", transport=httpx.MockTransport(handle)
    )
    result = await provider.respond([])
    assert result.usage == {"total_tokens": 12}
    assert result.request_id == "req"
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,error",
    [
        ("rate", ModelRetryableError),
        ("timeout", ModelOutcomeUnknown),
        ("invalid", ModelProtocolError),
    ],
)
async def test_deepseek_safe_error_classification(kind, error):
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if kind == "timeout":
            raise httpx.ReadTimeout("secret response", request=request)
        if kind == "rate":
            return httpx.Response(429, text="secret response")
        return httpx.Response(200, json={"secret": "response"})

    provider = DeepSeekProvider(
        "test-key", "model", transport=httpx.MockTransport(handle)
    )
    with pytest.raises(error) as caught:
        await provider.respond([])
    assert "secret" not in str(caught.value)
    assert calls == 1
