import pytest
from pydantic import ValidationError

from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.review import ReconciliationDecision, ToolBinding


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
