import json
from dataclasses import replace

import httpx
import pytest
from pydantic import ValidationError

from agentflow.application.agents.projection import project_context
from agentflow.application.ports.context_projection import ContextProjectionPolicy
from agentflow.application.ports.model_request import (
    ModelRequestContract,
    ModelRequestError,
    ModelRequestOptions,
    ProviderCapabilities,
    TokenCount,
)
from agentflow.infrastructure.models.deepseek import DeepSeekProvider
from agentflow.infrastructure.models.mock import MockProvider


def view(tools=None):
    messages = [{"role": "user", "content": "PRIVATE 中文 🧪"}]
    tools = tools or []
    return (
        messages,
        tools,
        project_context(messages, tools, ContextProjectionPolicy(), 100_000).manifest,
    )


def test_request_identity_is_frozen_verified_and_contains_no_prompt():
    prepared = MockProvider().prepare_request(*view())
    contract = prepared.request.contract
    assert contract.provider_key == "mock" and contract.model == "mock-v1"
    assert contract.max_output_tokens == 4096 and contract.stream is False
    assert "PRIVATE" not in contract.model_dump_json()
    assert "PRIVATE" not in repr(prepared)
    assert (
        ModelRequestContract.model_validate_json(contract.model_dump_json()) == contract
    )
    assert MockProvider().prepare_request(*view()).request.contract == contract
    with pytest.raises(ValidationError):
        contract.model = "forged"
    with pytest.raises(ValidationError):
        ModelRequestContract.model_validate(
            {**contract.model_dump(), "model": "forged"}
        )


@pytest.mark.parametrize("field", ["context_projection_digest", "tool_schema_digest"])
def test_forged_request_identity_cannot_dispatch(field):
    request = MockProvider().prepare_request(*view()).request
    # Even an independently valid digest cannot describe a different message view.
    context = json.dumps({"messages": [{"role": "user", "content": "changed"}]})
    if field == "tool_schema_digest":
        context = json.dumps({"messages": view()[0], "tools": [{"forged": True}]})
    with pytest.raises(ModelRequestError):
        replace(request, context_json=context).payload()


@pytest.mark.asyncio
async def test_deepseek_sends_reserved_model_output_and_endpoint_after_mutation():
    sent = []

    def handle(request):
        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    credential = "test-only-credential"
    provider = DeepSeekProvider(
        credential,
        "deepseek-chat",
        request_options=ModelRequestOptions(max_output_tokens=128),
        transport=httpx.MockTransport(handle),
    )
    prepared = provider.prepare_request(*view())
    if credential in prepared.request.contract.model_dump_json() or credential in repr(
        prepared
    ):
        pytest.fail("request identity exposed credentials")
    provider.model = "deepseek-flash"
    provider.base_url = "https://changed.example"
    provider.request_options = ModelRequestOptions(max_output_tokens=32)
    assert (await prepared.execute()).assistant_message.content == "ok"
    assert sent[0][0] == "https://api.deepseek.com/chat/completions"
    assert sent[0][1]["model"] == prepared.request.contract.model == "deepseek-chat"
    assert (
        sent[0][1]["max_tokens"] == prepared.request.contract.max_output_tokens == 128
    )


def test_tools_and_unknown_model_fail_before_network():
    tools = [
        {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}},
        }
    ]
    capabilities = ProviderCapabilities(
        provider_key="mock",
        model="mock-v1",
        capability_version="1",
        supports_tools=False,
        max_output_tokens=4096,
    )
    with pytest.raises(ModelRequestError, match="ModelCapabilityUnsupported"):
        MockProvider(capabilities=capabilities).prepare_request(*view(tools))
    with pytest.raises(ModelRequestError, match="ModelCapabilityUnsupported"):
        DeepSeekProvider("test-only", "unknown-model").prepare_request(*view())


def test_strict_window_requires_verified_counter():
    with pytest.raises(ModelRequestError, match="ModelTokenCountUnavailable"):
        MockProvider(
            request_options=ModelRequestOptions(strict_token_window=True)
        ).prepare_request(*view())


@pytest.mark.parametrize("assurance", ["exact", "upper_bound"])
def test_trusted_count_reserves_output_and_enforces_window(assurance):
    class Counter:
        def count(self, context_json):
            assert "PRIVATE" in context_json
            payload = json.loads(context_json)
            assert payload["model"] == "mock-v1"
            assert payload["max_tokens"] in {10, 11}
            assert payload["response_format"] == {"type": "text"}
            return TokenCount(input_tokens=90, assurance=assurance, method="fixture-v1")

    capabilities = ProviderCapabilities(
        provider_key="mock",
        model="mock-v1",
        capability_version="1",
        supports_tools=True,
        max_output_tokens=100,
        context_window_tokens=100,
        token_counting_method="fixture-v1",
        token_counting_assurance=assurance,
    )
    prepared = MockProvider(
        capabilities=capabilities,
        request_options=ModelRequestOptions(
            max_output_tokens=10, strict_token_window=True
        ),
        token_counter=Counter(),
    ).prepare_request(*view())
    assert prepared.request.contract.token_count.input_tokens == 90
    with pytest.raises(ModelRequestError, match="ModelTokenWindowExceeded"):
        MockProvider(
            capabilities=capabilities,
            request_options=ModelRequestOptions(
                max_output_tokens=11, strict_token_window=True
            ),
            token_counter=Counter(),
        ).prepare_request(*view())


@pytest.mark.parametrize(
    "parameters",
    [
        {"secret": "forbidden"},
        {"temperature": -1},
        {"temperature": float("nan")},
        {"top_p": 2},
    ],
)
def test_only_typed_bounded_parameters_are_accepted(parameters):
    with pytest.raises(ValidationError):
        ModelRequestOptions(parameters=parameters)


@pytest.mark.asyncio
async def test_legacy_output_configuration_cannot_exceed_adapter_limit():
    def forbidden(_):
        pytest.fail("invalid output configuration must not send a request")

    from agentflow.application.ports.model_provider import ModelConfigurationError

    provider = DeepSeekProvider(
        "test-only",
        "deepseek-chat",
        request_options=ModelRequestOptions(max_output_tokens=4097),
        transport=httpx.MockTransport(forbidden),
    )
    with pytest.raises(ModelConfigurationError):
        await provider.complete([])
