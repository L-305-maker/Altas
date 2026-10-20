"""Offline adapter conformance, not evidence of paid API acceptance."""

import json
from pathlib import Path

import httpx
import pytest

from agentflow.application.agents.projection import project_context
from agentflow.application.ports.context_projection import ContextProjectionPolicy
from agentflow.application.ports.model_provider import (
    ModelConfigurationError,
    ModelOutcomeUnknown,
    ModelProtocolError,
    ModelRateLimitedError,
    ModelRetryableError,
)
from agentflow.application.ports.model_request import (
    ModelRequestError,
    ModelRequestOptions,
)
from agentflow.bootstrap import build_provider
from agentflow.config import Settings
from agentflow.infrastructure.models.deepseek import DeepSeekProvider

MODELS = ("deepseek-flash", "deepseek-v4-pro")
LEGACY = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
)


def view():
    messages = [
        {"role": "user", "content": "PRIVATE_PROMPT"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "PRIVATE_REASONING",
            "tool_calls": [
                {
                    "id": "core-action-1",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "core-action-1", "content": "PRIVATE_RESULT"},
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}},
        }
    ]
    manifest = project_context(
        messages, tools, ContextProjectionPolicy(), 100_000
    ).manifest
    return messages, tools, manifest


def response():
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "opaque",
                    "tool_calls": [
                        {
                            "id": "vendor-2",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def test_new_deployment_default_is_current_model_without_changing_mock(monkeypatch):
    monkeypatch.delenv("AGENTFLOW_MODEL", raising=False)
    monkeypatch.delenv("AGENTFLOW_PROVIDER", raising=False)
    assert Settings().provider == "mock"
    assert Settings().model == "deepseek-flash"
    configured = build_provider(
        Settings(provider="deepseek", deepseek_api_key="synthetic-only")
    )
    assert configured.model == "deepseek-flash"
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    assert "AGENTFLOW_MODEL:-deepseek-flash" in compose.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("interface", ["prepare", "respond", "complete"])
async def test_all_interfaces_use_the_same_trusted_payload(model, interface):
    seen = []
    credential = "synthetic-key-never-send"

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=response())

    options = ModelRequestOptions(
        max_output_tokens=128,
        parameters={
            "temperature": 0.2,
            "top_p": 0.98,
            "response_format": "json_object",
        },
    )
    provider = DeepSeekProvider(
        credential,
        model,
        "https://api.deepseek.com/v1",
        request_options=options,
        transport=httpx.MockTransport(handle),
    )
    messages, tools, manifest = view()
    if interface == "prepare":
        prepared = provider.prepare_request(messages, tools, manifest)
        assert prepared.request.contract.model == model
        assert prepared.request.contract.max_output_tokens == 128
        assert "PRIVATE" not in repr(prepared)
        result = await prepared.execute()
        assert result.assistant_message.tool_calls[0].id == "vendor-2"
        assert result.assistant_message.reasoning_content == "opaque"
    else:
        result = await getattr(provider, interface)(messages, tools)
        if interface == "complete":
            assert result["tool_calls"][0]["id"] == "vendor-2"
        else:
            assert result.assistant_message.tool_calls[0].id == "vendor-2"
    assert len(seen) == 1
    request = seen[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
    assert payload == {
        "messages": messages,
        "tools": tools,
        "model": model,
        "max_tokens": 128,
        "stream": False,
        "temperature": 0.2,
        "top_p": 0.98,
        "response_format": {"type": "json_object"},
    }
    assert request.headers["Authorization"] == f"Bearer {credential}"
    if credential in str(request.url) or credential in json.dumps(payload):
        pytest.fail("CredentialOutsideAuthorization", pytrace=False)
    if interface == "prepare":
        assert payload == prepared.request.payload()
        assert credential not in prepared.request.contract.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", (*LEGACY, "unknown-model"))
async def test_lifecycle_refuses_unusable_names_before_network(model):
    from agentflow.infrastructure.models.deepseek import model_lifecycle

    expected = "legacy" if model in LEGACY else "unsupported"
    assert model_lifecycle(model) == expected

    def forbidden(_):
        pytest.fail("UnusableModelDispatched", pytrace=False)

    provider = DeepSeekProvider(
        "synthetic-only", model, transport=httpx.MockTransport(forbidden)
    )
    code = "ModelDeprecated" if expected == "legacy" else "ModelCapabilityUnsupported"
    with pytest.raises(ModelRequestError, match=code):
        provider.prepare_request(*view())
    for call in (provider.respond, provider.complete):
        with pytest.raises(ModelConfigurationError):
            await call(view()[0], view()[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
async def test_freeze_includes_sampling_format_schema_and_credential(model):
    captured = []
    credential = "synthetic-original"
    provider = DeepSeekProvider(
        credential,
        model,
        request_options=ModelRequestOptions(
            max_output_tokens=64,
            parameters={"top_p": 0.98, "response_format": "json_object"},
        ),
        transport=httpx.MockTransport(
            lambda r: captured.append(r) or httpx.Response(200, json=response())
        ),
    )
    messages, tools, manifest = view()
    prepared = provider.prepare_request(messages, tools, manifest)
    original = prepared.request.payload()
    provider.model = "changed-model"
    provider.base_url = "https://changed.example"
    provider._key = "synthetic-replacement"
    provider.request_options = ModelRequestOptions(max_output_tokens=1)
    messages[0]["content"] = "changed"
    tools[0]["function"]["name"] = "changed"
    await prepared.execute()
    assert json.loads(captured[0].content) == original
    assert str(captured[0].url) == "https://api.deepseek.com/chat/completions"
    assert captured[0].headers["Authorization"] == f"Bearer {credential}"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize(
    "kind,error",
    [
        (429, ModelRateLimitedError),
        (401, ModelConfigurationError),
        (422, ModelConfigurationError),
        (503, ModelOutcomeUnknown),
        ("read", ModelOutcomeUnknown),
        ("connect", ModelRetryableError),
        ("json", ModelProtocolError),
        ("structure", ModelProtocolError),
    ],
)
async def test_safe_errors_and_single_attempt(model, kind, error, caplog, capsys):
    caplog.set_level("DEBUG")
    calls = []

    def handle(request):
        calls.append(request)
        if kind == "read":
            raise httpx.ReadTimeout("PRIVATE_RESPONSE", request=request)
        if kind == "connect":
            raise httpx.ConnectTimeout("PRIVATE_RESPONSE", request=request)
        if kind == "json":
            return httpx.Response(200, text="PRIVATE_RESPONSE")
        if kind == "structure":
            return httpx.Response(200, json={"PRIVATE_RESPONSE": True})
        return httpx.Response(
            kind, text="PRIVATE_RESPONSE", headers={"Retry-After": "2"}
        )

    provider = DeepSeekProvider(
        "PRIVATE_CREDENTIAL", model, transport=httpx.MockTransport(handle)
    )
    prepared = provider.prepare_request(*view())
    with pytest.raises(error) as caught:
        await prepared.execute()
    assert len(calls) == 1
    output = capsys.readouterr()
    if "PRIVATE" in str(caught.value) + caplog.text + output.out + output.err:
        pytest.fail("ProviderDiagnosticContainsPrivateData", pytrace=False)
    if kind == 429:
        assert caught.value.retry_after_seconds == 2


@pytest.mark.parametrize(
    "options",
    [
        {"tools_enabled": False},
        {"strict_token_window": True},
        {"max_output_tokens": 4097},
    ],
)
def test_capability_and_window_reject_before_network(options):
    def forbidden(_):
        pytest.fail("RejectedRequestDispatched", pytrace=False)

    provider = DeepSeekProvider(
        "synthetic-only",
        MODELS[0],
        request_options=ModelRequestOptions(**options),
        transport=httpx.MockTransport(forbidden),
    )
    with pytest.raises(ModelRequestError):
        provider.prepare_request(*view())


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["?credential=PRIVATE", "#PRIVATE"])
async def test_all_interfaces_reject_private_endpoint_components(suffix):
    def forbidden(_):
        pytest.fail("PrivateEndpointDispatched", pytrace=False)

    provider = DeepSeekProvider(
        "PRIVATE_KEY",
        MODELS[0],
        base_url="https://api.deepseek.com" + suffix,
        transport=httpx.MockTransport(forbidden),
    )
    with pytest.raises(ModelRequestError, match="ModelEndpointUnsupported") as caught:
        provider.prepare_request(*view())
    assert "PRIVATE" not in str(caught.value)
    with pytest.raises(ModelConfigurationError):
        await provider.respond(view()[0], view()[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"tools_enabled": False},
        {"strict_token_window": True},
        {"max_output_tokens": 4097},
    ],
)
async def test_legacy_cannot_ignore_capability_or_strict_window(options):
    provider = DeepSeekProvider(
        "synthetic-only", MODELS[0], request_options=ModelRequestOptions(**options)
    )
    with pytest.raises(ModelConfigurationError):
        await provider.respond(view()[0], view()[1])
