"""同一套持久化契约在 SQLite 与专用 PostgreSQL 上验证。"""

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from agentflow.application.ports.agent_store import (
    ActionArguments,
    ActionObservation,
    AgentDataError,
    AgentMessage,
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.agent.status import ActionStatus, AgentStatus
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentActionRow,
    AgentMessageRow,
    AgentSessionRow,
    ArtifactRow,
    Base,
    StepRow,
)
from agentflow.infrastructure.persistence.store import Store


@pytest.fixture(params=["sqlite", "postgres"])
def agents(request, tmp_path):
    if request.param == "postgres":
        url = os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("requires a dedicated PostgreSQL test database")
    else:
        url = f"sqlite:///{(tmp_path / 'agent.db').as_posix()}"
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    store.create_schema()
    yield SQLAgentStore(store)
    Base.metadata.drop_all(store.engine)
    store.close()


def initial():
    return AgentSnapshot(
        id="agent-1",
        budget=AgentBudget(
            max_model_rounds=10,
            max_model_attempts=20,
            max_tool_calls=20,
            max_resource_units=10000,
        ),
    )


def start(agents, *, artifact=False, messages=()):
    store = agents.store
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="agent-test",
                steps=(
                    StepSpec(
                        id="work", handler="agent", artifact=artifact, retry_delay=0
                    ),
                ),
            )
        ),
        {},
    )
    task = store.claim(lease_seconds=300)
    assert task is not None and task.run_id == run
    return task, agents.open(task, initial(), messages=messages)


def candidate(snapshot, **changes):
    return AgentSnapshot.model_validate(
        {
            **snapshot.model_dump(),
            "revision": snapshot.revision + 1,
            **changes,
        }
    )


def reserve(agents, task, state, model_id="model-1"):
    old = state.snapshot
    return agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                old,
                status=AgentStatus.MODEL_INFLIGHT,
                model_call_id=model_id,
                actions=(),
                budget=old.budget.consume(
                    model_rounds=int(old.model_call_id != model_id),
                    model_attempts=1,
                ),
            )
        ),
    )


def response(state, *, action_id="action-1", arguments=None, sequence=None):
    old = state.snapshot
    arguments = {"query": "private input"} if arguments is None else arguments
    action = Action(
        id=action_id,
        agent_id=old.id,
        model_call_id=old.model_call_id,
        tool_name="lookup",
        arguments_digest=digest_arguments(arguments),
    )
    return AgentTransition(
        snapshot=candidate(old, status=AgentStatus.ACTIONS_PENDING, actions=(action,)),
        messages=(
            AgentMessage(
                sequence=len(state.messages) if sequence is None else sequence,
                role="assistant",
                model_call_id=old.model_call_id,
                payload={"content": None, "action_ids": [action_id]},
            ),
        ),
        model_result=ModelCallResult(
            model_call_id=old.model_call_id, status="succeeded", usage={"tokens": 10}
        ),
        arguments=(ActionArguments(action_id=action_id, arguments=arguments),),
    )


def propose(agents, task, state, **kwargs):
    return agents.commit_transition(task, response(state, **kwargs))


def dispatch(agents, task, state):
    old = state.snapshot
    return agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                old,
                actions=(old.actions[0].transition(ActionStatus.INFLIGHT),),
                budget=old.budget.consume(tool_calls=1),
            )
        ),
    )


def finish_action(agents, task, state):
    old = state.snapshot
    return agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                old, actions=(old.actions[0].transition(ActionStatus.SUCCEEDED),)
            ),
            observations=(
                ActionObservation(
                    sequence=len(state.messages),
                    action_id=old.actions[0].id,
                    result={"value": 42},
                ),
            ),
        ),
    )


def expire(agents, task):
    with agents.store.transaction() as db:
        db.execute(
            update(StepRow).where(StepRow.run_id == task.run_id).values(lease_until=0)
        )


def test_open_is_idempotent_and_binds_database_step_uuid(agents):
    message = AgentMessage(
        sequence=0, role="user", payload={"content": "private prompt"}
    )
    task, state = start(agents, messages=(message,))
    assert agents.open(task, initial(), messages=(message,)) == state
    with agents.store.transaction() as db:
        row = db.scalar(select(AgentSessionRow))
        step = db.get(StepRow, row.step_row_id)
        assert row.step_row_id != task.step_id
        assert step.key == task.step_id and step.run_id == task.run_id
        assert db.scalar(select(func.count()).select_from(AgentSessionRow)) == 1
    incompatible = AgentSnapshot.model_validate(
        {**initial().model_dump(), "id": "other"}
    )
    with pytest.raises(StateConflict):
        agents.open(task, incompatible)


def test_reopen_restores_messages_model_calls_and_historical_actions(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    state = propose(agents, task, state)
    state = finish_action(agents, task, dispatch(agents, task, state))
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot, status=AgentStatus.READY_MODEL)
        ),
    )
    state = reserve(agents, task, state, "model-2")
    url = agents.store.engine.url.render_as_string(hide_password=False)
    agents.store.close()
    reopened = Store(url)
    try:
        recovered = SQLAgentStore(reopened).load(task)
        assert recovered == state
        assert recovered.snapshot.actions == ()
        assert len(recovered.actions) == 1 and recovered.actions[0].result == {
            "value": 42
        }
        assert recovered.actions[0].idempotency_key == state.actions[0].idempotency_key
        assert [m.sequence for m in recovered.messages] == [0, 1]
        assert [m.role for m in recovered.messages] == ["assistant", "tool"]
        assert [m.model_call_id for m in recovered.model_calls] == [
            "model-1",
            "model-2",
        ]
        assert (
            AgentSnapshot.model_validate_json(recovered.snapshot.model_dump_json())
            == recovered.snapshot
        )
    finally:
        reopened.close()


def test_wrong_revision_and_duplicate_revision_are_conflicts(agents):
    task, state = start(agents)
    wrong = candidate(state.snapshot, revision=2)
    with pytest.raises(StateConflict):
        agents.commit_transition(task, AgentTransition(snapshot=wrong))
    changed = candidate(
        state.snapshot, budget=state.snapshot.budget.consume(resource_units=1)
    )
    agents.commit_transition(task, AgentTransition(snapshot=changed))
    with pytest.raises(StateConflict):
        agents.commit_transition(task, AgentTransition(snapshot=changed))


def test_two_independent_transactions_cannot_commit_the_same_revision(agents):
    task, state = start(agents)
    transition = AgentTransition(
        snapshot=candidate(
            state.snapshot, budget=state.snapshot.budget.consume(resource_units=1)
        )
    )
    barrier = Barrier(2)

    def commit(_):
        barrier.wait(timeout=5)
        try:
            agents.commit_transition(task, transition)
            return "committed"
        except StateConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(commit, range(2))) == ["committed", "conflict"]


def test_fencing_rejects_wrong_expired_and_replaced_tokens(agents):
    task, state = start(agents)
    with pytest.raises(LostLease):
        agents.load(replace(task, token="wrong"))
    expire(agents, task)
    with pytest.raises(LostLease):
        agents.commit_transition(
            task, AgentTransition(snapshot=candidate(state.snapshot))
        )
    new = agents.store.claim(300)
    assert new is not None and new.token != task.token
    assert agents.load(new).snapshot == state.snapshot
    with pytest.raises(LostLease):
        agents.commit_transition(
            task, AgentTransition(snapshot=candidate(state.snapshot))
        )


def test_bad_message_sequence_rolls_back_snapshot_model_result_and_actions(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    with pytest.raises(StateConflict):
        agents.commit_transition(task, response(state, sequence=5))
    assert agents.load(task) == state


def test_action_id_conflict_rolls_back_entire_model_response(agents):
    task, state = start(agents)
    state = finish_action(
        agents,
        task,
        dispatch(agents, task, propose(agents, task, reserve(agents, task, state))),
    )
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot, status=AgentStatus.READY_MODEL)
        ),
    )
    state = reserve(agents, task, state, "model-2")
    with pytest.raises(StateConflict):
        agents.commit_transition(task, response(state))
    assert agents.load(task) == state


def test_wrong_digest_and_missing_observation_are_rejected(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    data = response(state).model_dump()
    data["arguments"] = (
        ActionArguments(action_id="action-1", arguments={"wrong": True}),
    )
    with pytest.raises(AgentDataError):
        agents.commit_transition(task, AgentTransition.model_validate(data))
    assert agents.load(task) == state
    state = dispatch(agents, task, propose(agents, task, state))
    with pytest.raises(StateConflict):
        agents.commit_transition(
            task,
            AgentTransition(
                snapshot=candidate(
                    state.snapshot,
                    actions=(
                        state.snapshot.actions[0].transition(ActionStatus.SUCCEEDED),
                    ),
                )
            ),
        )
    assert agents.load(task) == state


def final_response(state):
    old = state.snapshot
    return AgentTransition(
        snapshot=candidate(old, status=AgentStatus.SUCCEEDED),
        messages=(
            AgentMessage(
                sequence=len(state.messages),
                role="assistant",
                model_call_id=old.model_call_id,
                payload={"content": "done", "action_ids": []},
            ),
        ),
        model_result=ModelCallResult(
            model_call_id=old.model_call_id, status="succeeded"
        ),
    )


def test_agent_step_and_artifact_commit_atomically_and_cannot_repeat(agents):
    task, state = start(agents, artifact=True)
    state = reserve(agents, task, state)
    transition = final_response(state)
    with pytest.raises(StateConflict):
        agents.commit_transition(task, transition)
    assert agents.load(task) == state
    final = agents.complete_agent_step(task, transition, {"answer": 42})
    assert final.snapshot.status == AgentStatus.SUCCEEDED
    assert agents.store.get_run(task.run_id)["status"] == "succeeded"
    with pytest.raises(LostLease):
        agents.complete_agent_step(task, transition, {"answer": 42})
    assert len(agents.store.artifacts(task.run_id)) == 1


def test_terminal_failure_does_not_restart_irreversible_agent(agents):
    task, state = start(agents)
    final = agents.fail_agent_step(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot, status=AgentStatus.EXHAUSTED)
        ),
    )
    assert final.snapshot.status == AgentStatus.EXHAUSTED
    assert agents.store.get_run(task.run_id)["steps"][0]["status"] == "failed"
    assert agents.store.claim() is None


def test_suspend_releases_lease_without_exposing_unconditional_resume(agents):
    task, state = start(agents)
    state = propose(agents, task, reserve(agents, task, state))
    waiting = agents.suspend_agent_step(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot, status=AgentStatus.WAITING_APPROVAL)
        ),
    )
    assert waiting.snapshot.status == AgentStatus.WAITING_APPROVAL
    with agents.store.transaction() as db:
        step = db.scalar(select(StepRow).where(StepRow.run_id == task.run_id))
        assert (
            step.status == "waiting" and step.token is None and step.lease_until is None
        )
    with pytest.raises(LostLease):
        agents.load(task)
    assert agents.store.claim() is None


def test_cancel_preserves_unknown_results_and_rejects_late_writes(agents):
    task, state = start(agents)
    state = dispatch(agents, task, propose(agents, task, reserve(agents, task, state)))
    agents.store.cancel(task.run_id)
    with agents.store.transaction() as db:
        saved = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
        assert saved.status == AgentStatus.CANCELLED
        assert saved.actions[0].status == ActionStatus.UNKNOWN
        assert db.scalar(select(AgentActionRow)).status == "unknown"
        assert db.scalar(select(func.count()).select_from(AgentMessageRow)) == 2
    with pytest.raises(LostLease):
        agents.load(task)
    assert "private input" not in str(agents.store.events(task.run_id))


def test_crash_after_tool_reservation_waits_for_reconciliation(agents):
    task, state = start(agents)
    state = dispatch(agents, task, propose(agents, task, reserve(agents, task, state)))
    expire(agents, task)
    assert agents.store.claim(300) is None
    with agents.store.transaction() as db:
        saved = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
        assert saved.status == AgentStatus.WAITING_RECONCILIATION
        assert saved.actions[0].status == ActionStatus.UNKNOWN
        assert db.scalar(select(StepRow)).status == "waiting"


def test_model_crash_recovery_keeps_call_id_and_accumulated_attempts(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    expire(agents, task)
    new = agents.store.claim(300)
    recovered = agents.load(new)
    assert recovered.model_calls[0].status == "unknown"
    assert recovered.snapshot.budget.model_attempts == 1
    ready = agents.commit_transition(
        new,
        AgentTransition(
            snapshot=candidate(recovered.snapshot, status=AgentStatus.READY_MODEL),
            model_result=ModelCallResult(model_call_id="model-1", status="failed"),
        ),
    )
    retried = reserve(agents, new, ready)
    assert retried.model_calls[0].attempts == 2
    assert retried.snapshot.budget.model_rounds == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_payloads_are_not_persisted(agents, value):
    task, state = start(agents)
    with pytest.raises(AgentDataError):
        agents.commit_transition(
            task,
            AgentTransition(
                snapshot=candidate(state.snapshot),
                messages=(
                    AgentMessage(sequence=0, role="user", payload={"value": value}),
                ),
            ),
        )
    assert agents.load(task) == state


def test_item_and_session_limits_are_enforced_without_partial_writes(
    agents, monkeypatch
):
    import agentflow.infrastructure.persistence.agent_store as module

    task, state = start(agents)
    message = AgentMessage(sequence=0, role="user", payload={"content": "x" * 101})
    monkeypatch.setattr(module, "MAX_MESSAGE_BYTES", 100)
    with pytest.raises(AgentDataError):
        agents.commit_transition(
            task,
            AgentTransition(snapshot=candidate(state.snapshot), messages=(message,)),
        )
    monkeypatch.setattr(module, "MAX_MESSAGE_BYTES", 100000)
    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 100)
    with pytest.raises(AgentDataError):
        agents.commit_transition(
            task, AgentTransition(snapshot=candidate(state.snapshot))
        )
    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 8000000)
    assert agents.load(task) == state


@pytest.mark.parametrize("target", ["message", "artifact"])
def test_database_insert_failure_rolls_back_all_related_state(agents, target):
    task, state = start(agents, artifact=True)
    state = reserve(agents, task, state)
    mapped = AgentMessageRow if target == "message" else ArtifactRow

    def reject(*_args):
        raise IntegrityError("injected", None, RuntimeError("private failure detail"))

    event.listen(mapped, "before_insert", reject)
    try:
        with pytest.raises(StateConflict) as caught:
            if target == "message":
                agents.commit_transition(task, response(state))
            else:
                agents.complete_agent_step(task, final_response(state), {"answer": 42})
        assert "private failure detail" not in str(caught.value)
    finally:
        event.remove(mapped, "before_insert", reject)
    assert agents.load(task) == state
    assert agents.store.get_run(task.run_id)["steps"][0]["status"] == "running"
    assert agents.store.artifacts(task.run_id) == []


@pytest.mark.parametrize("change", ["message_call", "message_actions", "result_call"])
def test_model_response_bindings_cannot_disagree(agents, change):
    task, state = start(agents)
    state = reserve(agents, task, state)
    data = response(state).model_dump()
    if change == "message_call":
        data["messages"][0]["model_call_id"] = "different"
    elif change == "message_actions":
        data["messages"][0]["payload"]["action_ids"] = ["different"]
    else:
        data["model_result"]["model_call_id"] = "different"
    with pytest.raises(StateConflict):
        agents.commit_transition(task, AgentTransition.model_validate(data))
    assert agents.load(task) == state


def test_workflow_complete_cannot_bypass_agent_terminal_transition(agents):
    from agentflow.infrastructure.persistence.store import ConflictError

    task, state = start(agents)
    with pytest.raises(ConflictError):
        agents.store.complete(task, {"bypass": True})
    assert agents.load(task) == state


def test_artifact_setting_comes_from_persisted_spec(agents):
    task, state = start(agents, artifact=True)
    state = reserve(agents, task, state)
    forged = replace(task, spec=StepSpec(id="work", handler="agent", artifact=False))
    agents.complete_agent_step(forged, final_response(state), {"answer": 42})
    assert len(agents.store.artifacts(task.run_id)) == 1


def test_existing_worker_failure_records_unknown_and_does_not_retry_agent(agents):
    task, state = start(agents)
    state = dispatch(agents, task, propose(agents, task, reserve(agents, task, state)))
    assert agents.store.fail(task, RuntimeError("private failure detail"))
    with agents.store.transaction() as db:
        snapshot = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
        assert snapshot.status == AgentStatus.FAILED
        assert snapshot.actions[0].status == ActionStatus.UNKNOWN
    assert agents.store.claim() is None
    assert "private failure detail" not in str(agents.store.events(task.run_id))


def test_arbitrary_exception_class_names_are_reduced_to_safe_codes(agents):
    task, state = start(agents)
    dispatch(agents, task, propose(agents, task, reserve(agents, task, state)))
    unusual = type("invalid exception name " + "x" * 100, (Exception,), {})
    assert agents.store.fail(task, unusual("private detail"))
    with agents.store.transaction() as db:
        snapshot = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
        assert snapshot.actions[0].error_code == "AgentExecutionError"


@pytest.mark.parametrize("deadline", [None, float("inf"), float("nan")])
def test_invalid_lease_is_rejected_as_lost_lease(agents, deadline):
    task, _state = start(agents)
    with agents.store.transaction() as db:
        db.execute(
            update(StepRow)
            .where(StepRow.run_id == task.run_id)
            .values(lease_until=deadline)
        )
    with pytest.raises(LostLease):
        agents.load(task)


def test_driver_errors_do_not_expose_payload_parameters(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)

    def reject(*_args):
        raise OperationalError(
            "insert", {"prompt": "private content"}, RuntimeError("private error")
        )

    event.listen(AgentMessageRow, "before_insert", reject)
    try:
        with pytest.raises(AgentStoreUnavailable) as caught:
            agents.commit_transition(task, response(state))
        assert "private" not in str(caught.value)
    finally:
        event.remove(AgentMessageRow, "before_insert", reject)
    assert agents.load(task) == state


def test_session_quota_keeps_room_to_record_unknown_on_cancellation(
    agents, monkeypatch
):
    import agentflow.infrastructure.persistence.agent_store as module

    task, state = start(agents)
    state = dispatch(agents, task, propose(agents, task, reserve(agents, task, state)))
    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 5000)
    for _ in range(50):
        try:
            state = agents.commit_transition(
                task,
                AgentTransition(
                    snapshot=candidate(state.snapshot),
                    messages=(
                        AgentMessage(
                            sequence=len(state.messages),
                            role="user",
                            payload={"content": "x" * 100},
                        ),
                    ),
                ),
            )
        except AgentDataError:
            break
    else:
        pytest.fail("session quota was never reached")
    agents.store.cancel(task.run_id)
    with agents.store.transaction() as db:
        saved = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
        assert saved.status == AgentStatus.CANCELLED
        assert saved.actions[0].status == ActionStatus.UNKNOWN
