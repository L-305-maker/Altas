import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_context import session
from test_disclosure_binding import PRIVATE, PUBLIC, binding, bound_history, v3_policy

from agentflow.application.agents.checkpoint import build_checkpoint
from agentflow.application.agents.context import (
    AgentContextBuilder,
    UnresolvedActions,
    context_bytes,
)
from agentflow.application.agents.egress import prepare_egress
from agentflow.application.agents.projection import project_context
from agentflow.application.ports.agent_store import AgentSession
from agentflow.application.ports.context_egress import ProviderContextEgressPolicy
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionPolicy,
)
from agentflow.infrastructure.models.mock import MockProvider


def policy(**changes):
    return ProviderContextEgressPolicy(
        **{
            "disclosure_mode": "bound_disclosure",
            "rules": v3_policy().disclosure.rules,
            **changes,
        }
    )


def project(
    state, egress_policy, *, mode="full", checkpoint=None, limit=100_000, tools=None
):
    full = AgentContextBuilder().build(state)
    egress = prepare_egress(full, state, egress_policy, checkpoint)
    return project_context(
        full,
        tools or [],
        ContextProjectionPolicy(mode=mode),
        limit,
        egress=egress,
        checkpoint=checkpoint,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["same", "version", "digest", "missing"])
async def test_binding_decision_is_used_by_actual_provider_in_both_plain_modes(change):
    actual = binding(version="v2") if change == "version" else binding()
    if change == "digest":
        actual = type(actual).model_validate(
            {**actual.model_dump(), "tool_policy_digest": "0" * 64}
        )
    if change == "missing":
        actual = None
    state = bound_history(actual, rounds=3)
    original = state.model_dump_json()
    for mode in ("full", "recent_complete_rounds"):
        projected = project(state, policy(), mode=mode)
        provider = MockProvider()
        prepared = provider.prepare_request(projected.messages, [], projected.manifest)
        await prepared.execute()
        context = provider.contexts[0]
        raw = json.dumps(context, ensure_ascii=False)
        assert PUBLIC in raw
        assert (PRIVATE in raw) == (change == "same")
        assert "SYNTHETIC_NESTED_PRIVATE" not in raw
        assert context[0] == AgentContextBuilder().build(state)[0]
        for observation in (m for m in context if m["role"] == "tool"):
            saved = next(
                a for a in state.actions if a.action.id == observation["tool_call_id"]
            )
            content = json.loads(observation["content"])
            assert content["status"] == saved.action.status
            assert content["error_code"] == saved.action.error_code
        prepared.request.contract.verify_projection(projected.manifest)
        prepared.request.contract.verify_context(context, [])
        assert prepared.request.contract.context_egress_digest is not None
        assert PRIVATE not in projected.manifest.model_dump_json()
    assert state.model_dump_json() == original


def test_no_policy_preserves_legacy_bytes_and_raw_observations():
    state = bound_history(binding())
    projected = project(state, None)
    assert projected.messages == AgentContextBuilder().build(state)
    assert PRIVATE in json.dumps(projected.messages)
    assert "egress" not in projected.manifest.model_dump(mode="json")
    contract = (
        MockProvider()
        .prepare_request(projected.messages, [], projected.manifest)
        .request.contract
    )
    assert "context_egress_digest" not in contract.model_dump(mode="json")
    safe = project(state, ProviderContextEgressPolicy())
    assert safe.manifest != projected.manifest
    assert safe.manifest.full_context_digest == projected.manifest.full_context_digest


@pytest.mark.parametrize("classification", ["unknown", "sensitive"])
def test_nonpublic_rule_cannot_disclose(classification):
    rule = policy().rules[0].model_dump()
    selected = policy(
        rules=[{**rule, "mode": "metadata_only", "classification": classification}]
    )
    assert PUBLIC not in json.dumps(
        project(bound_history(binding()), selected).messages
    )
    with pytest.raises(ValidationError):
        policy(rules=[{**rule, "classification": classification}])


@pytest.mark.parametrize("status", ["unknown", "proposed", "inflight"])
def test_full_history_blocks_unresolved_even_when_oldest_round_would_be_omitted(status):
    if status == "unknown":
        state = session(("unknown",), rounds=3)
    else:
        from test_context_projection import history

        # The pure DTO permits historical pending actions; full replay rejects all.
        data = history(rounds=3).model_dump()
        data["actions"][0]["action"]["status"] = status
        state = type(session()).model_validate(data)
    with pytest.raises(UnresolvedActions):
        project(
            state, ProviderContextEgressPolicy(), mode="recent_complete_rounds", limit=1
        )


def test_denied_has_no_public_raw_result_and_does_not_invent_approval():
    data = bound_history(binding()).model_dump()
    for stored in data["actions"]:
        stored["action"]["status"] = "denied"
        stored["action"]["error_code"] = "ToolDenied"
    for action in data["snapshot"]["actions"]:
        action["status"] = "denied"
        action["error_code"] = "ToolDenied"
    data["snapshot"]["budget"]["tool_calls"] = 0
    state = type(session()).model_validate(data)
    result = project(state, policy())
    assert PUBLIC not in json.dumps(result.messages) and PRIVATE not in json.dumps(
        result.messages
    )
    assert all(
        json.loads(m["content"])["status"] == "denied"
        for m in result.messages
        if m["role"] == "tool"
    )


def test_v3_uses_one_unchanged_renderer_or_rejects_conflicting_policy():
    state = bound_history(binding(version="v2"))
    checkpoint_policy = v3_policy()
    checkpoint = build_checkpoint(state, checkpoint_policy)
    baseline = project(
        state, None, mode="evidence_anchored_recent_rounds", checkpoint=checkpoint
    )
    compatible = project(
        state, policy(), mode="evidence_anchored_recent_rounds", checkpoint=checkpoint
    )
    assert compatible.messages == baseline.messages
    assert compatible.manifest.checkpoint == baseline.manifest.checkpoint
    with pytest.raises(
        ContextProjectionError, match="AgentContextEgressPolicyConflict"
    ):
        project(
            state,
            ProviderContextEgressPolicy(),
            mode="evidence_anchored_recent_rounds",
            checkpoint=checkpoint,
        )
    metadata_checkpoint = build_checkpoint(
        state, v3_policy(disclosure={"policy_version": 2})
    )
    metadata = project(
        state,
        ProviderContextEgressPolicy(),
        mode="evidence_anchored_recent_rounds",
        checkpoint=metadata_checkpoint,
    )
    assert PUBLIC not in json.dumps(metadata.messages) and PRIVATE not in json.dumps(
        metadata.messages
    )


def test_large_history_has_bounded_serialization_search_and_exact_utf8_limit(
    monkeypatch,
):
    import agentflow.application.agents.projection as module

    state = bound_history(binding(), rounds=10)
    data = state.model_dump()
    for stored in data["actions"]:
        stored["result"]["nested"]["password"] = "合成私密数据" * 4000
    state = type(state).model_validate(data)
    full = AgentContextBuilder().build(state)
    egress = prepare_egress(full, state, ProviderContextEgressPolicy())
    schemas = [
        {"type": "function", "function": {"name": "read", "description": "中文"}}
    ]
    one = [egress.messages[0], *egress.messages[-3:]]
    exact = context_bytes(one, schemas)
    calls = 0
    original = module.context_bytes

    def count(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(module, "context_bytes", count)
    result = project_context(
        full,
        schemas,
        ContextProjectionPolicy(mode="recent_complete_rounds"),
        exact,
        egress=egress,
    )
    assert result.messages == one and result.manifest.omitted_round_count == 9
    assert result.manifest.projected_context_bytes == exact
    assert calls == 2  # Constant context serialization count, not a machine-time SLO.
    with pytest.raises(ContextProjectionError, match="AgentContextLimit"):
        project_context(
            full,
            schemas,
            ContextProjectionPolicy(mode="recent_complete_rounds"),
            exact - 1,
            egress=egress,
        )


def test_oversized_authorized_field_fails_without_truncating_or_widening():
    state = bound_history(binding())
    with pytest.raises(ContextProjectionError, match="AgentToolDisclosureLimit"):
        project(state, policy(max_disclosure_bytes=1))
    assert PUBLIC in json.dumps([a.result for a in state.actions])


def test_rules_require_complete_bound_identity_and_no_legacy_full_opt_in():
    for data in (
        {"policy_version": True},
        {"disclosure_mode": "legacy_full"},
        {"rules": policy().rules},
        {
            "disclosure_mode": "bound_disclosure",
            "rules": [{"tool_name": "read", "result_path": ["value"]}],
        },
    ):
        with pytest.raises(ValidationError):
            ProviderContextEgressPolicy.model_validate(data)


def test_user_assistant_reasoning_and_call_arguments_are_not_claimed_as_result_dlp():
    data = bound_history(binding()).model_dump()
    data["messages"][0]["payload"]["content"] = "SYNTHETIC_USER_PRIVATE"
    assistant = data["messages"][1]["payload"]["message"]
    assistant["content"] = "SYNTHETIC_ASSISTANT_PRIVATE"
    assistant["reasoning_content"] = "SYNTHETIC_REASONING_PRIVATE"
    state = type(session()).model_validate(data)
    before = AgentContextBuilder().build(state)
    prepared = prepare_egress(before, state, ProviderContextEgressPolicy())
    assert "SYNTHETIC_USER_PRIVATE" not in repr(prepared)
    result = project(state, ProviderContextEgressPolicy())
    assert all(
        original == selected
        for original, selected in zip(before, result.messages, strict=True)
        if original["role"] != "tool"
    )
    assert PRIVATE not in json.dumps(
        [m for m in result.messages if m["role"] == "tool"]
    )


def test_reviewed_v3_fixture_keeps_exact_manifest_contract_and_wire_context_bytes():
    from agentflow.application.ports.evidence_checkpoint import EvidenceCheckpointPolicy

    captured = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/checkpoint_v3_b0f4a7a.json"
        ).read_text(encoding="utf-8")
    )
    state = AgentSession.model_validate(captured["state"])
    checkpoint = build_checkpoint(state, EvidenceCheckpointPolicy(**captured["policy"]))
    result = project(
        state, None, mode="evidence_anchored_recent_rounds", checkpoint=checkpoint
    )
    prepared = MockProvider().prepare_request(result.messages, [], result.manifest)
    assert result.manifest.model_dump_json() == captured["manifest_json"]
    assert prepared.request.contract.model_dump_json() == captured["contract_json"]
    assert prepared.request.context_json == captured["context_json"]
