import pytest
from pydantic import ValidationError

from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.agent.status import ActionStatus, AgentStatus
from agentflow.domain.execution.exception import InvalidStateTransitionError


@pytest.fixture
def snapshot():
    return AgentSnapshot(
        id="agent-1",
        budget=AgentBudget(
            max_model_rounds=2,
            max_model_attempts=3,
            max_tool_calls=3,
            max_resource_units=100,
        ),
    )


@pytest.fixture
def pending(snapshot):
    action = Action(
        id="action-1",
        agent_id=snapshot.id,
        model_call_id="model-1",
        tool_name="lookup",
        arguments_digest=digest_arguments({"x": 1}),
    )
    return AgentSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "status": AgentStatus.ACTIONS_PENDING,
            "model_call_id": "model-1",
            "actions": (action,),
            "budget": snapshot.budget.consume(model_rounds=1, model_attempts=1),
        }
    )


@pytest.mark.parametrize("status", list(AgentStatus))
def test_all_agent_states_roundtrip(snapshot, pending, status):
    data = pending.model_dump()
    data["status"] = status
    if status in {AgentStatus.MODEL_INFLIGHT, AgentStatus.SUCCEEDED}:
        data["actions"] = ()
    elif status == AgentStatus.READY_MODEL:
        data = snapshot.model_dump()
    elif status == AgentStatus.WAITING_RECONCILIATION:
        data["actions"] = (
            pending.actions[0]
            .transition(ActionStatus.INFLIGHT)
            .transition(ActionStatus.UNKNOWN, error_code="LeaseExpired"),
        )
        data["budget"] = pending.budget.consume(tool_calls=1)
    value = AgentSnapshot.model_validate(data)
    assert AgentSnapshot.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "changes",
    [
        {"agent_id": "another-agent"},
        {"model_call_id": "another-model"},
    ],
)
def test_snapshot_rejects_actions_from_another_execution(pending, changes):
    foreign = Action.model_validate({**pending.actions[0].model_dump(), **changes})
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate({**pending.model_dump(), "actions": (foreign,)})


def test_action_ids_must_be_unique_in_current_batch(pending):
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate(
            {**pending.model_dump(), "actions": pending.actions * 2}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": AgentStatus.MODEL_INFLIGHT},
        {"status": AgentStatus.SUCCEEDED},
        {"status": AgentStatus.READY_MODEL},
        {"status": AgentStatus.WAITING_RECONCILIATION},
        {"actions": ()},
        {"model_call_id": None},
        {"revision": -1},
        {"revision": True},
        {"unexpected": "field"},
    ],
)
def test_snapshot_invariants_are_checked_on_restore(pending, changes):
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate({**pending.model_dump(), **changes})


def test_executed_actions_require_charged_tool_calls(pending):
    action = pending.actions[0].transition(ActionStatus.INFLIGHT)
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate({**pending.model_dump(), "actions": (action,)})


def test_unknown_requires_reconciliation_and_preserves_execution_facts(pending):
    unknown = (
        pending.actions[0]
        .transition(ActionStatus.INFLIGHT)
        .transition(ActionStatus.UNKNOWN, error_code="LeaseExpired")
    )
    data = {
        **pending.model_dump(),
        "actions": (unknown,),
        "budget": pending.budget.consume(tool_calls=1),
    }
    for status in [AgentStatus.ACTIONS_PENDING, AgentStatus.WAITING_APPROVAL]:
        with pytest.raises(ValidationError):
            AgentSnapshot.model_validate({**data, "status": status})
    for status in [
        AgentStatus.WAITING_RECONCILIATION,
        AgentStatus.CANCELLED,
        AgentStatus.FAILED,
        AgentStatus.EXHAUSTED,
    ]:
        value = AgentSnapshot.model_validate({**data, "status": status})
        assert value.actions[0].status == ActionStatus.UNKNOWN


def test_finished_batch_can_return_to_model_and_waiting_requires_proposals(pending):
    denied = pending.actions[0].transition(ActionStatus.DENIED, error_code="NotAllowed")
    data = {**pending.model_dump(), "actions": (denied,)}
    ready = AgentSnapshot.model_validate({**data, "status": AgentStatus.READY_MODEL})
    assert ready.actions == (denied,)
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate({**data, "status": AgentStatus.WAITING_APPROVAL})


def test_model_identity_requires_charged_round(snapshot):
    with pytest.raises(ValidationError):
        AgentSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "status": AgentStatus.MODEL_INFLIGHT,
                "model_call_id": "model-1",
            }
        )


def test_snapshot_and_nested_values_are_immutable(pending):
    with pytest.raises(ValidationError):
        pending.revision = 2
    with pytest.raises(ValidationError):
        pending.budget.tool_calls = 1
    with pytest.raises(ValidationError):
        pending.actions[0].id = "different"
    assert isinstance(pending.actions, tuple)


def successor(snapshot, **changes):
    return AgentSnapshot.model_validate(
        {**snapshot.model_dump(), "revision": snapshot.revision + 1, **changes}
    )


def test_snapshot_advances_through_model_and_action_cycle(snapshot):
    model = snapshot.advance(
        successor(
            snapshot,
            status=AgentStatus.MODEL_INFLIGHT,
            model_call_id="model-1",
            budget=snapshot.budget.consume(model_rounds=1, model_attempts=1),
        )
    )
    action = Action(
        id="action-1",
        agent_id=model.id,
        model_call_id=model.model_call_id,
        tool_name="lookup",
        arguments_digest=digest_arguments({}),
    )
    pending = model.advance(
        successor(model, status=AgentStatus.ACTIONS_PENDING, actions=(action,))
    )
    inflight = pending.advance(
        successor(
            pending,
            actions=(action.transition(ActionStatus.INFLIGHT),),
            budget=pending.budget.consume(tool_calls=1),
        )
    )
    completed = inflight.advance(
        successor(
            inflight, actions=(inflight.actions[0].transition(ActionStatus.SUCCEEDED),)
        )
    )
    ready = completed.advance(successor(completed, status=AgentStatus.READY_MODEL))
    final_model = ready.advance(
        successor(
            ready,
            status=AgentStatus.MODEL_INFLIGHT,
            model_call_id="model-2",
            actions=(),
            budget=ready.budget.consume(model_rounds=1, model_attempts=1),
        )
    )
    final = final_model.advance(successor(final_model, status=AgentStatus.SUCCEEDED))
    assert final.revision == 7
    assert final.budget.tool_calls == 1
    assert snapshot.revision == snapshot.budget.model_rounds == 0


@pytest.mark.parametrize("changes", [{"id": "other"}, {"revision": 0}, {"revision": 2}])
def test_snapshot_successor_preserves_identity_and_increments_revision(
    snapshot, changes
):
    with pytest.raises(ValueError):
        snapshot.advance(successor(snapshot, **changes))


@pytest.mark.parametrize("status", [s for s in AgentStatus if s.is_terminal])
def test_terminal_snapshot_rejects_all_further_writes(snapshot, pending, status):
    source = pending if status == AgentStatus.SUCCEEDED else snapshot
    final = successor(source, status=status, actions=())
    with pytest.raises(InvalidStateTransitionError):
        final.advance(successor(final))
    with pytest.raises(InvalidStateTransitionError):
        final.advance(successor(final, status=AgentStatus.READY_MODEL))


@pytest.mark.parametrize("resource", ["tool_calls", "resource_units", "model_attempts"])
def test_snapshot_cannot_refund_budget_or_change_limits(pending, resource):
    charged = successor(pending, budget=pending.budget.consume(**{resource: 1}))
    with pytest.raises(ValueError):
        charged.advance(successor(charged, budget=pending.budget))
    changed_limit = AgentBudget.model_validate(
        {
            **pending.budget.model_dump(),
            f"max_{resource}": getattr(pending.budget, f"max_{resource}") + 1,
        }
    )
    with pytest.raises(ValueError):
        pending.advance(successor(pending, budget=changed_limit))


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "replacement"},
        {"tool_name": "different_tool"},
        {"arguments_digest": digest_arguments({"other": 1})},
        {"side_effect": "read_only"},
        {"status": ActionStatus.SUCCEEDED},
    ],
)
def test_snapshot_cannot_replace_or_skip_action_execution(pending, changes):
    replacement = Action.model_validate(
        {
            **pending.actions[0].model_dump(),
            "status": ActionStatus.INFLIGHT,
            **changes,
        }
    )
    candidate = successor(
        pending, actions=(replacement,), budget=pending.budget.consume(tool_calls=1)
    )
    with pytest.raises((ValueError, InvalidStateTransitionError)):
        pending.advance(candidate)


def test_snapshot_cannot_reopen_completed_action(pending):
    denied = pending.actions[0].transition(ActionStatus.DENIED, error_code="NotAllowed")
    completed = successor(pending, actions=(denied,))
    with pytest.raises(InvalidStateTransitionError):
        completed.advance(successor(completed, actions=pending.actions))


def test_snapshot_reconciles_without_executing_again(pending):
    unknown = (
        pending.actions[0]
        .transition(ActionStatus.INFLIGHT)
        .transition(ActionStatus.UNKNOWN, error_code="LeaseExpired")
    )
    waiting = successor(
        pending,
        status=AgentStatus.WAITING_RECONCILIATION,
        actions=(unknown,),
        budget=pending.budget.consume(tool_calls=1),
    )
    resolved = waiting.advance(
        successor(
            waiting,
            status=AgentStatus.ACTIONS_PENDING,
            actions=(unknown.reconcile(ActionStatus.SUCCEEDED),),
        )
    )
    assert resolved.budget.tool_calls == waiting.budget.tool_calls
    with pytest.raises(InvalidStateTransitionError):
        waiting.advance(
            successor(
                waiting,
                status=AgentStatus.ACTIONS_PENDING,
                actions=(pending.actions[0].transition(ActionStatus.INFLIGHT),),
            )
        )


def test_new_model_call_requires_new_round_and_attempt(snapshot):
    started = successor(
        snapshot,
        status=AgentStatus.MODEL_INFLIGHT,
        model_call_id="model-1",
        budget=snapshot.budget.consume(model_rounds=1, model_attempts=1),
    )
    retry_ready = successor(started, status=AgentStatus.READY_MODEL)
    with pytest.raises(ValueError):
        retry_ready.advance(
            successor(
                retry_ready, status=AgentStatus.MODEL_INFLIGHT, model_call_id="new"
            )
        )
    with pytest.raises(ValueError):
        started.advance(successor(started, model_call_id="different"))


def test_new_actions_must_be_proposals_and_only_follow_model_response(pending):
    model = successor(pending, status=AgentStatus.MODEL_INFLIGHT, actions=())
    inflight = pending.actions[0].transition(ActionStatus.INFLIGHT)
    with pytest.raises(ValueError):
        model.advance(
            successor(
                model,
                status=AgentStatus.ACTIONS_PENDING,
                actions=(inflight,),
                budget=model.budget.consume(tool_calls=1),
            )
        )
    with pytest.raises(InvalidStateTransitionError):
        pending.advance(
            successor(pending, status=AgentStatus.MODEL_INFLIGHT, actions=())
        )


def test_confirmed_model_failure_can_retry_without_starting_another_round(snapshot):
    model = snapshot.advance(
        successor(
            snapshot,
            status=AgentStatus.MODEL_INFLIGHT,
            model_call_id="model-1",
            budget=snapshot.budget.consume(model_rounds=1, model_attempts=1),
        )
    )
    ready = model.advance(successor(model, status=AgentStatus.READY_MODEL))
    retried = ready.advance(
        successor(
            ready,
            status=AgentStatus.MODEL_INFLIGHT,
            budget=ready.budget.consume(model_attempts=1),
        )
    )
    assert retried.model_call_id == model.model_call_id
    assert retried.budget.model_rounds == 1
    assert retried.budget.model_attempts == 2
    with pytest.raises(ValueError):
        ready.advance(successor(ready, status=AgentStatus.MODEL_INFLIGHT))


def test_model_round_cannot_be_charged_without_a_new_call(pending):
    with pytest.raises(ValueError):
        pending.advance(
            successor(
                pending,
                budget=pending.budget.consume(model_rounds=1, model_attempts=1),
            )
        )


def test_illegal_snapshot_state_transition_rejects_structurally_valid_candidate(
    snapshot,
):
    candidate = successor(
        snapshot,
        status=AgentStatus.SUCCEEDED,
        model_call_id="model-1",
        budget=snapshot.budget.consume(model_rounds=1, model_attempts=1),
    )
    with pytest.raises(InvalidStateTransitionError):
        snapshot.advance(candidate)


def test_previous_tool_usage_cannot_pay_for_new_dispatches(pending):
    previous_rounds = successor(pending, budget=pending.budget.consume(tool_calls=1))
    dispatch = successor(
        previous_rounds,
        actions=(previous_rounds.actions[0].transition(ActionStatus.INFLIGHT),),
    )
    with pytest.raises(ValueError):
        previous_rounds.advance(dispatch)
    charged = successor(
        previous_rounds,
        actions=dispatch.actions,
        budget=previous_rounds.budget.consume(tool_calls=1),
    )
    assert previous_rounds.advance(charged).budget.tool_calls == 2
