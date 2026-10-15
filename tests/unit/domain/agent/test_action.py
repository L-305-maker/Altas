import json

import pytest
from pydantic import ValidationError

from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.status import ActionStatus, ToolSideEffect
from agentflow.domain.execution.exception import InvalidStateTransitionError


@pytest.fixture
def action():
    return Action(
        id="action-1",
        agent_id="agent-1",
        model_call_id="model-1",
        tool_name="lookup",
        arguments_digest=digest_arguments({"query": "example"}),
    )


def test_action_execution_is_immutable_and_restorable(action):
    assert action.side_effect == ToolSideEffect.UNSAFE
    inflight = action.transition(ActionStatus.INFLIGHT)
    succeeded = inflight.transition(ActionStatus.SUCCEEDED)
    assert action.status == ActionStatus.PROPOSED
    assert succeeded.id == action.id
    assert succeeded.arguments_digest == action.arguments_digest
    assert Action.model_validate_json(succeeded.model_dump_json()) == succeeded
    with pytest.raises(ValidationError):
        action.status = ActionStatus.INFLIGHT


@pytest.mark.parametrize("target", [ActionStatus.FAILED, ActionStatus.UNKNOWN])
def test_execution_errors_use_codes(action, target):
    result = action.transition(ActionStatus.INFLIGHT).transition(
        target, error_code="ToolTimeout"
    )
    assert result.error_code == "ToolTimeout"
    assert Action.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "status", [ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.DENIED]
)
@pytest.mark.parametrize("target", list(ActionStatus))
def test_completed_action_cannot_complete_or_execute_again(action, status, target):
    if status == ActionStatus.DENIED:
        result = action.transition(status, error_code="NotAllowed")
    else:
        result = action.transition(ActionStatus.INFLIGHT).transition(
            status, error_code="ToolError" if status == ActionStatus.FAILED else None
        )
    assert result.status.is_terminal
    with pytest.raises(InvalidStateTransitionError):
        result.transition(target, error_code="ToolError")


@pytest.mark.parametrize("side_effect", list(ToolSideEffect))
@pytest.mark.parametrize("target", list(ActionStatus))
def test_unknown_never_automatically_executes_or_completes(action, side_effect, target):
    proposed = Action.model_validate(
        {**action.model_dump(), "side_effect": side_effect}
    )
    unknown = proposed.transition(ActionStatus.INFLIGHT).transition(
        ActionStatus.UNKNOWN, error_code="LeaseExpired"
    )
    restored = Action.model_validate_json(unknown.model_dump_json())
    with pytest.raises(InvalidStateTransitionError):
        restored.transition(target)


@pytest.mark.parametrize("target", [ActionStatus.SUCCEEDED, ActionStatus.FAILED])
def test_unknown_requires_explicit_reconciliation(action, target):
    unknown = action.transition(ActionStatus.INFLIGHT).transition(
        ActionStatus.UNKNOWN, error_code="TransportLost"
    )
    result = unknown.reconcile(
        target, error_code="ConfirmedFailure" if target == ActionStatus.FAILED else None
    )
    assert result.status == target
    assert unknown.status == ActionStatus.UNKNOWN
    with pytest.raises(InvalidStateTransitionError):
        result.reconcile(target)


@pytest.mark.parametrize("target", [s for s in ActionStatus if not s.is_terminal])
def test_reconciliation_cannot_reopen_execution(action, target):
    unknown = action.transition(ActionStatus.INFLIGHT).transition(
        ActionStatus.UNKNOWN, error_code="LeaseExpired"
    )
    with pytest.raises(InvalidStateTransitionError):
        unknown.reconcile(target)


@pytest.mark.parametrize(
    "current,target",
    [
        (ActionStatus.PROPOSED, ActionStatus.SUCCEEDED),
        (ActionStatus.PROPOSED, ActionStatus.UNKNOWN),
        (ActionStatus.INFLIGHT, ActionStatus.PROPOSED),
        (ActionStatus.INFLIGHT, ActionStatus.DENIED),
    ],
)
def test_illegal_action_transitions(action, current, target):
    current_action = (
        action if current == ActionStatus.PROPOSED else action.transition(current)
    )
    with pytest.raises(InvalidStateTransitionError):
        current_action.transition(target)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"agent_id": ""},
        {"model_call_id": ""},
        {"arguments_digest": "raw arguments"},
        {"status": "failed"},
        {"status": "unknown"},
        {"status": "denied"},
        {"error_code": "ToolError"},
        {"status": "succeeded", "error_code": "ToolError"},
        {"status": "failed", "error_code": "sensitive error text"},
        {"unexpected": True},
    ],
)
def test_action_rejects_inconsistent_or_unstructured_data(action, changes):
    with pytest.raises(ValidationError):
        Action.model_validate({**action.model_dump(), **changes})


def test_argument_digest_is_canonical_and_does_not_store_input(action):
    assert digest_arguments({"b": [1, "中文"], "a": None}) == digest_arguments(
        {"a": None, "b": [1, "中文"]}
    )
    assert digest_arguments({"x": 1}) != digest_arguments({"x": 2})
    assert "example" not in json.dumps(action.model_dump(mode="json"))
    with pytest.raises(ValueError):
        digest_arguments({"x": float("nan")})
