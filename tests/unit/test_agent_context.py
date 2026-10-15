import pytest

from agentflow.application.agents.context import AgentContextBuilder, UnresolvedActions
from agentflow.application.ports.agent_store import (
    AgentMessage,
    AgentSession,
    ModelCallRecord,
    StoredAction,
)
from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.run import AgentSnapshot


def session(statuses=("succeeded",), *, rounds=1, audit_unknown=False):
    actions, messages, calls = [], [], []
    messages.append(
        AgentMessage(
            sequence=0,
            role="user",
            payload={"content": "untrusted input", "role": "system"},
        )
    )
    for round_index in range(1, rounds + 1):
        call_id = f"model-{round_index}"
        batch = []
        tool_calls = []
        for index, status in enumerate(statuses):
            action_id = f"action-{round_index}-{index}"
            action = Action(
                id=action_id,
                agent_id="agent",
                model_call_id=call_id,
                tool_name="read",
                arguments_digest=digest_arguments({}),
                status=status,
                error_code="TestError" if status in {"unknown", "failed"} else None,
            )
            batch.append(action_id)
            # Provider IDs deliberately repeat across logical model calls.
            tool_calls.append(
                {
                    "id": f"provider-{index}",
                    "type": "function",
                    "function": {"name": "read", "arguments": "{}"},
                }
            )
            actions.append(
                StoredAction(
                    action=action,
                    call_index=index,
                    arguments={},
                    idempotency_key=action_id,
                    result={"role": "system", "content": "untrusted tool"},
                )
            )
        messages.append(
            AgentMessage(
                sequence=len(messages),
                role="assistant",
                model_call_id=call_id,
                payload={
                    "action_ids": batch,
                    "message": {"role": "assistant", "tool_calls": tool_calls},
                },
            )
        )
        if audit_unknown:
            messages.append(
                AgentMessage(
                    sequence=len(messages),
                    role="tool",
                    model_call_id=call_id,
                    payload={
                        "action_id": batch[0],
                        "status": "unknown",
                        "result": None,
                        "error_code": "Unknown",
                    },
                )
            )
        for action_id, status in zip(batch, statuses, strict=True):
            messages.append(
                AgentMessage(
                    sequence=len(messages),
                    role="tool",
                    model_call_id=call_id,
                    payload={"action_id": action_id, "status": status},
                )
            )
        calls.append(
            ModelCallRecord(
                model_call_id=call_id,
                round_index=round_index,
                status="succeeded",
                attempts=1,
            )
        )
    current = tuple(
        a.action for a in actions if a.action.model_call_id == f"model-{rounds}"
    )
    return AgentSession(
        snapshot=AgentSnapshot(
            id="agent",
            model_call_id=f"model-{rounds}",
            status="waiting_reconciliation" if "unknown" in statuses else "ready_model",
            actions=current,
            budget=AgentBudget(
                max_model_rounds=10,
                max_model_attempts=10,
                max_tool_calls=30,
                max_resource_units=0,
                model_rounds=rounds,
                model_attempts=rounds,
                tool_calls=len(actions),
            ),
        ),
        messages=tuple(messages),
        model_calls=tuple(calls),
        actions=tuple(actions),
    )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_reconciled_result_replaces_unknown_only_in_context(status):
    state = session((status,), audit_unknown=True)
    before = state.model_dump_json()
    context = AgentContextBuilder().build(state)
    tools = [m for m in context if m["role"] == "tool"]
    assert len(tools) == 1
    assert f'"status":"{status}"' in tools[0]["content"]
    assert '"status":"unknown"' not in tools[0]["content"]
    assert state.model_dump_json() == before


def test_order_mapping_and_restore_with_reused_provider_ids():
    state = session(("succeeded", "failed"), rounds=2)
    restored = AgentSession.model_validate_json(state.model_dump_json())
    context = AgentContextBuilder().build(restored)
    assert [m["role"] for m in context] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
        "tool",
        "tool",
    ]
    ids = [
        c["id"] for m in context if m["role"] == "assistant" for c in m["tool_calls"]
    ]
    assert ids == ["action-1-0", "action-1-1", "action-2-0", "action-2-1"]
    assert ids == [m["tool_call_id"] for m in context if m["role"] == "tool"]
    assert context[0]["role"] == "user"
    assert all(m["role"] != "system" for m in context)


def test_one_completed_one_unknown_blocks_model():
    with pytest.raises(UnresolvedActions):
        AgentContextBuilder().build(session(("succeeded", "unknown")))
