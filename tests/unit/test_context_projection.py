import json

import pytest
from pydantic import ValidationError
from test_agent_context import session

from agentflow.application.agents.context import AgentContextBuilder, UnresolvedActions
from agentflow.application.agents.projection import (
    ContextProjectionError,
    project_context,
)
from agentflow.application.ports.context_projection import ContextProjectionPolicy


def history(statuses=("succeeded",), **kwargs):
    state = session(tuple("failed" if s == "denied" else s for s in statuses), **kwargs)
    if "denied" not in statuses:
        return state
    data = state.model_dump()
    for collection in (
        data["snapshot"]["actions"],
        [a["action"] for a in data["actions"]],
    ):
        for action in collection:
            if statuses[int(action["id"].rsplit("-", 1)[1])] == "denied":
                action["status"] = "denied"
    data["snapshot"]["budget"]["tool_calls"] -= statuses.count("denied") * kwargs.get(
        "rounds", 1
    )
    return type(state).model_validate(data)


def project(state, *, tools=None, limit=100_000, **policy):
    # Always validate the complete persisted history before selecting a suffix.
    return project_context(
        AgentContextBuilder().build(state),
        tools or [],
        ContextProjectionPolicy(**policy),
        limit,
    )


def test_full_is_compatible_and_manifest_is_deterministic():
    state = session(("succeeded", "failed"), rounds=3)
    before = state.model_dump_json()
    full = AgentContextBuilder().build(state)
    projection = project(state)
    assert projection.messages == full
    assert projection.manifest.included_round_count == 3
    assert projection.manifest.omitted_round_count == 0
    assert projection == project(state)
    assert state.model_dump_json() == before
    assert "untrusted" not in projection.manifest.model_dump_json()


@pytest.mark.parametrize("statuses", [("succeeded", "failed"), ("denied", "succeeded")])
def test_recent_rounds_are_indivisible_ordered_suffixes(statuses):
    state = history(statuses, rounds=3, audit_unknown="denied" not in statuses)
    full = project(state)
    one = project(history(statuses)).manifest.projected_context_bytes
    projected = project(state, mode="recent_complete_rounds", limit=one)
    assert projected.manifest.included_round_count == 1
    assert projected.manifest.omitted_round_count == 2
    assert projected.messages == [full.messages[0], *full.messages[-3:]]
    assert [m["tool_call_id"] for m in projected.messages[2:]] == [
        "action-3-0",
        "action-3-1",
    ]
    assert all(m["role"] != "system" for m in projected.messages)
    assert projected.manifest.full_context_digest == full.manifest.full_context_digest
    assert (
        projected.manifest.projected_context_digest
        != full.manifest.projected_context_digest
    )


def test_exact_utf8_boundary_includes_schemas_and_required_context():
    messages = [
        {"role": "system", "content": "trusted"},
        {"role": "user", "content": "中文 🧪"},
        {"role": "assistant", "content": "旧历史" * 20},
        {"role": "user", "content": "保留原始用户数据"},
        {"role": "assistant", "content": "最近"},
    ]
    tools = [
        {
            "type": "function",
            "function": {"name": "工具", "parameters": {"type": "object"}},
        }
    ]
    policy = ContextProjectionPolicy(mode="recent_complete_rounds")
    expected = [messages[0], messages[1], *messages[3:]]
    size = len(
        json.dumps(
            {"messages": expected, "tools": tools},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    projected = project_context(messages, tools, policy, size)
    assert projected.messages == expected
    assert projected.manifest.projected_context_bytes == size
    with pytest.raises(ContextProjectionError, match="AgentContextLimit"):
        project_context(messages, tools, policy, size - 1)
    assert (
        project_context(messages, tools, policy, 100_000).manifest.tool_schema_digest
        != project_context(messages, [], policy, 100_000).manifest.tool_schema_digest
    )


@pytest.mark.parametrize("status", ["unknown", "proposed", "inflight"])
def test_omission_cannot_hide_unresolved_old_actions(status):
    state = session(("succeeded",), rounds=2)
    data = state.model_dump()
    data["actions"][0]["action"]["status"] = status
    if status == "unknown":
        data["actions"][0]["action"]["error_code"] = "Unknown"
    with pytest.raises(UnresolvedActions):
        project(
            type(state).model_validate(data), mode="recent_complete_rounds", limit=1
        )


def test_full_history_corruption_cannot_be_hidden():
    state = session(rounds=2)
    data = state.model_dump()
    data["messages"][1]["payload"]["action_ids"] = ["forged"]
    from agentflow.application.ports.agent_store import StateConflict

    with pytest.raises(StateConflict):
        project(
            type(state).model_validate(data), mode="recent_complete_rounds", limit=1
        )


def test_required_round_count_and_large_message_fail_without_truncation():
    state = session(rounds=3)
    one = project(session()).manifest.projected_context_bytes
    with pytest.raises(ContextProjectionError, match="AgentContextLimit"):
        project(state, mode="recent_complete_rounds", min_recent_rounds=2, limit=one)
    with pytest.raises(ContextProjectionError, match="AgentContextLimit"):
        project(state, limit=one)


def test_omitted_history_changes_full_identity_but_not_selected_identity():
    policy = ContextProjectionPolicy(mode="recent_complete_rounds")
    first = [
        {"role": "system", "content": "trusted"},
        {"role": "user", "content": "goal"},
        {"role": "assistant", "content": "a" * 1000},
        {"role": "assistant", "content": "recent"},
    ]
    second = [*first[:2], {"role": "assistant", "content": "b" * 1000}, first[-1]]
    a = project_context(first, [], policy, 200).manifest
    b = project_context(second, [], policy, 200).manifest
    assert a.omitted_round_count == b.omitted_round_count == 1
    assert a.projected_context_digest == b.projected_context_digest
    assert a.full_context_digest != b.full_context_digest


def test_digest_is_canonical_history_sensitive_and_versioned():
    a = [{"role": "user", "content": "a"}]
    b = [{"content": "a", "role": "user"}]
    policy = ContextProjectionPolicy()
    first = project_context(a, [], policy, 1000).manifest
    assert first == project_context(b, [], policy, 1000).manifest
    assert (
        first.full_context_digest
        != project_context(
            [{"role": "user", "content": "b"}], [], policy, 1000
        ).manifest.full_context_digest
    )
    changed = type(first).model_validate(
        {**first.model_dump(), "projection_version": 2}
    )
    assert changed != first
    with pytest.raises(ValidationError):
        ContextProjectionPolicy(projection_version=2)
    with pytest.raises(ValidationError):
        policy.mode = "recent_complete_rounds"


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "orphan", "content": "x"}],
        [{"role": "assistant", "tool_calls": [{"id": "a"}]}],
        [
            {"role": "assistant", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "b", "content": "x"},
        ],
    ],
)
def test_projection_validates_protocol_again(messages):
    with pytest.raises(ContextProjectionError, match="AgentContextProjectionInvalid"):
        project_context(messages, [], ContextProjectionPolicy(), 1000)
