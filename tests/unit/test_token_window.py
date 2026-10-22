import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_model_request_contract import view

from agentflow.application.ports.model_request import (
    ModelRequestContract,
    ModelRequestError,
    ModelRequestOptions,
    TokenCount,
    check_request_continuity,
)
from agentflow.infrastructure.models.mock import MockProvider


def test_unprofiled_counter_cannot_claim_exact():
    from token_counter_fixtures import capabilities

    class Unbound:
        def count(self, _):
            return TokenCount(input_tokens=1, assurance="exact", method="fixture-v1")

    with pytest.raises(ModelRequestError, match="ModelTokenCounterInvalid"):
        MockProvider(
            request_options=ModelRequestOptions(max_output_tokens=10),
            capabilities=capabilities(),
            token_counter=Unbound(),
        ).prepare_request(*view())


@pytest.mark.parametrize(
    "change",
    [
        {"provider_key": "other"},
        {"model": "other"},
        {"adapter_version": "other"},
        {"capability_version": "other"},
        {"token_counting_method": "other"},
        {"counting_assurance": "upper_bound"},
        {"context_window_tokens": 101},
        {"max_output_tokens": 99},
    ],
)
def test_profile_must_match_all_capabilities_before_counting(change):
    from token_counter_fixtures import Counter, capabilities, profile

    counter = Counter(profile=profile(**change))
    with pytest.raises(ModelRequestError, match="ModelTokenCounterInvalid"):
        MockProvider(
            request_options=ModelRequestOptions(max_output_tokens=10),
            capabilities=capabilities(),
            token_counter=counter,
        ).prepare_request(*view())
    assert counter.payloads == []


@pytest.mark.parametrize("assurance", ["exact", "upper_bound"])
def test_profile_and_output_boundary_are_frozen_in_explicit_v2(assurance):
    from token_counter_fixtures import Counter, capabilities, profile

    counter = Counter(profile=profile(counting_assurance=assurance))
    provider = MockProvider(
        capabilities=capabilities(token_counting_assurance=assurance),
        token_counter=counter,
        request_options=ModelRequestOptions(
            max_output_tokens=10, strict_token_window=True
        ),
    )
    prepared = provider.prepare_request(*view())
    contract = prepared.request.contract
    assert contract.request_contract_version == 2
    assert contract.token_counter_profile == counter.profile
    assert contract.reserved_output_tokens == 10
    assert (
        ModelRequestContract.model_validate_json(contract.model_dump_json()) == contract
    )
    assert json.loads(counter.payloads[0]) == prepared.request.payload()
    provider.request_options = ModelRequestOptions(
        max_output_tokens=11, strict_token_window=True
    )
    with pytest.raises(ModelRequestError, match="ModelTokenWindowExceeded"):
        provider.prepare_request(*view())


@pytest.mark.parametrize(
    "field",
    [
        "profile_version",
        "chat_template_version",
        "framing_version",
        "tokenizer_asset_digest",
    ],
)
def test_profile_drift_cannot_reuse_logical_request(field):
    from token_counter_fixtures import Counter, capabilities, profile

    provider = MockProvider(
        request_options=ModelRequestOptions(max_output_tokens=10),
        capabilities=capabilities(),
        token_counter=Counter(),
    )
    old = provider.prepare_request(*view()).request.contract
    value = "0" * 64 if field.endswith("digest") else "2"
    provider.token_counter = Counter(profile=profile(**{field: value}))
    current = provider.prepare_request(*view()).request.contract
    with pytest.raises(ModelRequestError, match="ModelRequestContractChanged"):
        check_request_continuity(
            [SimpleNamespace(request_contract=old)], current, retry=True
        )


def test_v1_identity_remains_byte_for_byte_and_unknown_version_rejected():
    contract = MockProvider().prepare_request(*view()).request.contract
    assert contract.request_contract_version == 1
    assert "token_counter_profile" not in contract.model_dump()
    assert (
        contract.request_contract_digest
        == "65d6e5192c88ddc63aa76642491cbeec81b4276471fde69ca0299720bf5378fb"
    )
    assert (
        ModelRequestContract.model_validate_json(contract.model_dump_json()) == contract
    )
    data = contract.model_dump()
    data["request_contract_version"] = 3
    with pytest.raises(ValidationError):
        ModelRequestContract.model_validate(data)


@pytest.mark.parametrize("version", [True, False, "1", "2"])
def test_contract_version_remains_a_strict_integer(version):
    contract = MockProvider().prepare_request(*view()).request.contract
    with pytest.raises(ValidationError):
        ModelRequestContract.model_validate(
            {**contract.model_dump(), "request_contract_version": version}
        )


def test_count_errors_are_safe_and_do_not_expose_request(capsys):
    from token_counter_fixtures import Counter, capabilities

    class Broken(Counter):
        def count(self, payload):
            raise ValueError("PRIVATE test-key " + payload)

    with pytest.raises(ModelRequestError) as error:
        MockProvider(
            request_options=ModelRequestOptions(max_output_tokens=10),
            capabilities=capabilities(),
            token_counter=Broken(),
        ).prepare_request(*view())
    assert str(error.value) == "ModelTokenCounterInvalid"
    assert error.value.__cause__ is None
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_oversized_counter_input_rejected_before_counting():
    from token_counter_fixtures import Counter, capabilities

    from agentflow.application.agents.projection import project_context
    from agentflow.application.ports.context_projection import ContextProjectionPolicy

    messages = [{"role": "user", "content": "x" * 8_100_000}]
    projection = project_context(
        messages, [], ContextProjectionPolicy(), 8_200_000
    ).manifest
    counter = Counter()
    with pytest.raises(ModelRequestError, match="ModelTokenCounterInvalid"):
        MockProvider(
            capabilities=capabilities(),
            token_counter=counter,
            request_options=ModelRequestOptions(max_output_tokens=10),
        ).prepare_request(messages, [], projection)
    assert not counter.payloads


def test_counter_uses_already_frozen_context_during_concurrent_mutation(monkeypatch):
    from token_counter_fixtures import Counter, capabilities

    import agentflow.application.ports.model_request as module

    messages, tools, manifest = view()
    canonical, armed = module.canonical_json, True

    def mutate_after_freeze(value):
        nonlocal armed
        frozen = canonical(value)
        if armed and isinstance(value, dict) and value.get("messages") is messages:
            armed = False
            messages[0]["content"] = "changed-after-freeze"
        return frozen

    monkeypatch.setattr(module, "canonical_json", mutate_after_freeze)
    counter = Counter()
    prepared = MockProvider(
        capabilities=capabilities(),
        token_counter=counter,
        request_options=ModelRequestOptions(max_output_tokens=10),
    ).prepare_request(messages, tools, manifest)
    assert json.loads(counter.payloads[0]) == prepared.request.payload()


def test_conformance_checks_assets_and_independent_expected_vectors():
    from token_counter_fixtures import ASSET, Counter

    from agentflow.infrastructure.models.token_conformance import (
        TokenCounterVector,
        verify_counter_conformance,
    )

    vector = TokenCounterVector(
        request_json='{"model":"mock-v1","messages":[{"role":"user","content":"公开 🧪 <special>"}]}',
        input_tokens=90,
    )
    kwargs = dict(
        tokenizer_asset=ASSET,
        chat_template_asset=ASSET,
        framing_asset=ASSET,
        evidence=ASSET,
        vectors=(vector,),
    )
    verify_counter_conformance(Counter(), **kwargs)
    for field in [
        "tokenizer_asset",
        "chat_template_asset",
        "framing_asset",
        "evidence",
    ]:
        with pytest.raises(ModelRequestError, match="ModelTokenCounterInvalid"):
            verify_counter_conformance(Counter(), **{**kwargs, field: b"corrupt"})
    with pytest.raises(ModelRequestError):
        verify_counter_conformance(
            Counter(), **{**kwargs, "vectors": (replace(vector, input_tokens=91),)}
        )


def test_profile_requires_reviewed_asset_and_evidence_identities():
    from token_counter_fixtures import profile

    original = profile()
    with pytest.raises(ValidationError):
        original.model = "another-model"
    for change in (
        {"evidence_digest": None},
        {"tokenizer_asset_digest": "not-sha256"},
        {"chat_template_digest": None},
        {"framing_digest": None},
        {"counting_assurance": "unknown"},
        {"context_window_tokens": True},
        {"prompt": "not-configuration"},
    ):
        with pytest.raises(ValidationError):
            profile(**change)


@pytest.mark.asyncio
async def test_counter_sees_full_frozen_wire_including_protocol_and_options():
    from token_counter_fixtures import Counter, capabilities

    from agentflow.application.agents.projection import project_context
    from agentflow.application.ports.context_projection import ContextProjectionPolicy

    calls = [
        {
            "id": f"core-{i}",
            "type": "function",
            "function": {"name": f"tool{i}", "arguments": '{"值":"🧪"}'},
        }
        for i in range(2)
    ]
    messages = [
        {"role": "system", "content": "公开规范 <special>"},
        {"role": "user", "content": "你好 🧪"},
        {"role": "assistant", "content": "", "tool_calls": calls},
        *[
            {"role": "tool", "tool_call_id": call["id"], "content": '{"结果":"公开"}'}
            for call in calls
        ],
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool{i}",
                "description": "工具描述" * 100,
                "parameters": {
                    "type": "object",
                    "properties": {"值": {"type": "string"}},
                },
            },
        }
        for i in range(2)
    ]
    manifest = project_context(
        messages, tools, ContextProjectionPolicy(), 100_000
    ).manifest
    counter = Counter()
    provider = MockProvider(
        capabilities=capabilities(),
        token_counter=counter,
        request_options=ModelRequestOptions(
            max_output_tokens=10,
            strict_token_window=True,
            parameters={"response_format": "json_object"},
        ),
    )
    prepared = provider.prepare_request(messages, tools, manifest)
    expected = {
        "messages": messages,
        "tools": tools,
        "model": "mock-v1",
        "max_tokens": 10,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    expected = deepcopy(expected)
    assert json.loads(counter.payloads[0]) == expected
    messages[0]["content"] = "hot-mutated"
    tools.clear()
    provider.request_options = ModelRequestOptions(max_output_tokens=99)
    await prepared.execute()
    assert prepared.request.payload() == expected
    assert len(counter.payloads) == 1


def test_upper_bound_conformance_checks_against_independent_actual_count():
    from token_counter_fixtures import ASSET, Counter, profile

    from agentflow.infrastructure.models.token_conformance import (
        TokenCounterVector,
        verify_counter_conformance,
    )

    kwargs = dict(
        tokenizer_asset=ASSET,
        chat_template_asset=ASSET,
        framing_asset=ASSET,
        evidence=ASSET,
        vectors=(TokenCounterVector('{"model":"mock-v1","messages":[]}', 90),),
    )
    verify_counter_conformance(
        Counter(profile=profile(counting_assurance="upper_bound"), tokens=91), **kwargs
    )
    with pytest.raises(ModelRequestError):
        verify_counter_conformance(
            Counter(profile=profile(counting_assurance="upper_bound"), tokens=89),
            **kwargs,
        )
