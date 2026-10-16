import pytest
from pydantic import ValidationError

from agentflow.application.tools.runtime import (
    Tool,
    ToolDeniedError,
    ToolInvocation,
    ToolRuntime,
)
from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.review import (
    ActionApproval,
    ActionExecutionContext,
    ReconciliationDecision,
    ToolBinding,
)


def test_binding_cannot_authorize_changed_action():
    binding = ToolBinding(
        tool_name="write",
        tool_version="1",
        tool_policy_digest="a" * 64,
        side_effect="unsafe",
        requires_approval=True,
    )
    action = Action(
        id="a",
        agent_id="s",
        model_call_id="m",
        tool_name="write",
        arguments_digest=digest_arguments({}),
    )
    assert binding.protected
    assert binding.matches(action)
    changed = Action.model_validate({**action.model_dump(), "tool_name": "other"})
    assert not binding.matches(changed)


@pytest.mark.parametrize(
    "changes",
    [
        {"confirmed_status": "unknown"},
        {"confirmed_status": "failed"},
        {"error_code": "Unexpected"},
        {"result": float("nan")},
        {"actor": "  "},
        {"evidence_reference": ""},
    ],
)
def test_reconciliation_requires_explicit_safe_evidence(changes):
    with pytest.raises(ValidationError):
        ReconciliationDecision.model_validate(
            {
                "request_id": "request",
                "expected_revision": 1,
                "confirmed_status": "succeeded",
                "actor": "operator-label",
                "reason": "checked",
                "evidence_reference": "external:receipt",
                **changes,
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"consumed_at": 1},
        {"actor": "operator"},
        {"decision": "approved"},
        {"decision": "cancelled"},
        {
            "decision": "rejected",
            "actor": "operator",
            "decided_at": 1,
            "consumed_at": 2,
        },
    ],
)
def test_approval_rejects_inconsistent_decision_facts(changes):
    with pytest.raises(ValidationError):
        ActionApproval.model_validate(
            {
                "approval_id": "approval",
                "session_id": "session",
                "action_id": "action",
                "binding": {
                    "tool_name": "write",
                    "tool_version": "1",
                    "tool_policy_digest": "a" * 64,
                    "side_effect": "unsafe",
                    "requires_approval": True,
                },
                "arguments_digest": digest_arguments({}),
                "decision": "pending",
                "created_at": 0,
                **changes,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["proposed", "succeeded", "failed", "unknown", "denied"]
)
async def test_runtime_cannot_invoke_action_without_inflight_reservation(status):
    runtime = ToolRuntime()
    calls = []

    async def invoke(args):
        calls.append(args)

    runtime.register(
        Tool("write", "write", {"type": "object"}, invoke, requires_approval=True)
    )
    binding = runtime.binding("write")
    action = Action(
        id="a",
        agent_id="s",
        model_call_id="m",
        tool_name="write",
        arguments_digest=digest_arguments({}),
        status=status,
        error_code="Stopped" if status in {"failed", "unknown", "denied"} else None,
    )
    approval = ActionApproval(
        approval_id="approval",
        session_id="s",
        action_id="a",
        binding=binding,
        arguments_digest=action.arguments_digest,
        decision="approved",
        actor="operator",
        created_at=0,
        decided_at=1,
        consumed_at=2,
    )
    with pytest.raises(ToolDeniedError):
        await runtime.call_durable(
            "write",
            {},
            ToolInvocation("a", "stable"),
            allowed={"write"},
            side_effect=action.side_effect,
            context=ActionExecutionContext(
                action=action,
                binding=binding,
                approval=approval,
                idempotency_key="stable",
            ),
        )
    assert not calls
