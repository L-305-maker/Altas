import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select, update

from agentflow.application.ports.agent_store import (
    ActionArguments,
    ActionObservation,
    AgentMessage,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.domain.agent.action import Action, digest_arguments
from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.review import (
    ApprovalDecision,
    ReconciliationDecision,
    ToolBinding,
)
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentActionApprovalRow,
    AgentActionReconciliationRow,
    AgentActionRow,
    AgentSessionRow,
    Base,
)
from agentflow.infrastructure.persistence.store import Store


@pytest.fixture(params=["sqlite", "postgres"])
def agents(request, tmp_path):
    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if request.param == "postgres"
        else f"sqlite:///{(tmp_path / 'reviews.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database")
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    store.create_schema()
    yield SQLAgentStore(store)
    Base.metadata.drop_all(store.engine)
    store.close()


def candidate(state, **changes):
    return AgentSnapshot.model_validate(
        {**state.model_dump(), "revision": state.revision + 1, **changes}
    )


def batch(agents, *, protected=True, count=1):
    store = agents.store
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="review",
                steps=(StepSpec(id="work", handler="agent", retry_delay=0),),
            )
        ),
        {},
    )
    task = store.claim(300)
    state = agents.open(
        task,
        AgentSnapshot(
            id="session",
            budget=AgentBudget(
                max_model_rounds=5,
                max_model_attempts=8,
                max_tool_calls=8,
                max_resource_units=0,
            ),
        ),
    )
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                state.snapshot,
                status="model_inflight",
                model_call_id="model",
                budget=state.snapshot.budget.consume(model_rounds=1, model_attempts=1),
            )
        ),
    )
    binding = ToolBinding(
        tool_name="write",
        tool_version="v1",
        tool_policy_digest="a" * 64,
        side_effect="unsafe" if protected else "read_only",
        requires_approval=protected,
    )
    actions = tuple(
        Action(
            id=f"action-{i}",
            agent_id="session",
            model_call_id="model",
            tool_name="write",
            side_effect=binding.side_effect,
            arguments_digest=digest_arguments({}),
        )
        for i in range(count)
    )
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                state.snapshot, status="actions_pending", actions=actions
            ),
            model_result=ModelCallResult(model_call_id="model", status="succeeded"),
            messages=(
                AgentMessage(
                    sequence=0,
                    role="assistant",
                    model_call_id="model",
                    payload={"action_ids": [a.id for a in actions]},
                ),
            ),
            arguments=tuple(
                ActionArguments(action_id=a.id, arguments={}, tool_binding=binding)
                for a in actions
            ),
        ),
    )
    return run, task, state


def decision(state, value="approved", **changes):
    return ApprovalDecision(
        expected_revision=state.snapshot.revision,
        decision=value,
        actor="operator-label",
        reason="checked",
        **changes,
    )


def wait_unknown(agents, count=1):
    run, task, state = batch(agents, protected=False, count=count)
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                state.snapshot,
                actions=tuple(a.transition("inflight") for a in state.snapshot.actions),
                budget=state.snapshot.budget.consume(tool_calls=count),
            )
        ),
    )
    state = agents.suspend_agent_step(
        task,
        AgentTransition(
            snapshot=candidate(
                state.snapshot,
                status="waiting_reconciliation",
                actions=tuple(
                    a.transition("unknown", error_code="ToolUnknown")
                    for a in state.snapshot.actions
                ),
            ),
            observations=tuple(
                ActionObservation(
                    sequence=len(state.messages) + i, action_id=a.id, result=None
                )
                for i, a in enumerate(state.snapshot.actions)
            ),
        ),
    )
    return run, task, state


def reconcile(state, index=0, status="succeeded", request_id="request"):
    return ReconciliationDecision(
        request_id=request_id,
        expected_revision=state.snapshot.revision,
        confirmed_status=status,
        error_code="ConfirmedFailed" if status == "failed" else None,
        result={"value": 3},
        actor="operator-label",
        reason="checked",
        evidence_reference="receipt:123",
    )


def test_approval_wait_decision_and_atomic_consumption(agents):
    run, task, state = batch(agents)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)
    approval = state.approvals[0]
    assert state.snapshot.status == "waiting_approval"
    assert agents.store.get_run(run)["steps"][0]["status"] == "waiting"
    assert agents.store.claim() is None
    result = agents.decide_action(approval.approval_id, decision(state))
    assert agents.decide_action(approval.approval_id, decision(state)) == result
    with pytest.raises(StateConflict):
        agents.decide_action(approval.approval_id, decision(state, "rejected"))
    new = agents.store.claim(300)
    assert new.token != task.token
    with pytest.raises(LostLease):
        agents.load(task)
    state = agents.load(new)
    transition = AgentTransition(
        snapshot=candidate(
            state.snapshot,
            actions=(state.snapshot.actions[0].transition("inflight"),),
            budget=state.snapshot.budget.consume(tool_calls=1),
        )
    )
    with pytest.raises(StateConflict):
        agents.commit_transition(new, transition)
    authorized = AgentTransition.model_validate(
        {**transition.model_dump(), "authorizations": (approval.approval_id,)}
    )
    saved = agents.commit_transition(new, authorized)
    assert saved.approvals[0].consumed_at is not None
    assert saved.snapshot.budget.model_attempts == 1
    assert saved.snapshot.actions[0].id == "action-0"


def test_rejection_appends_observation_and_restores_step(agents):
    run, task, state = batch(agents)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)
    agents.decide_action(state.approvals[0].approval_id, decision(state, "rejected"))
    new = agents.store.claim(300)
    state = agents.load(new)
    assert state.snapshot.actions[0].status == "denied"
    assert state.messages[-1].payload["error_code"] == "ActionRejected"
    assert state.snapshot.budget.tool_calls == 0


def test_reconciliation_resumes_only_after_last_unknown(agents):
    run, task, state = wait_unknown(agents, count=2)
    record = agents.reconcile_action("session", "action-0", reconcile(state))
    assert record.applied
    assert agents.reconcile_action("session", "action-0", reconcile(state)) == record
    assert agents.store.claim(300) is None
    with agents.store.transaction() as db:
        snapshot = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
    assert snapshot.status == "waiting_reconciliation"
    request = ReconciliationDecision.model_validate(
        {
            **reconcile(state, status="failed", request_id="second").model_dump(),
            "expected_revision": snapshot.revision,
        }
    )
    agents.reconcile_action("session", "action-1", request)
    new = agents.store.claim(300)
    restored = agents.load(new)
    assert restored.snapshot.status == "actions_pending"
    assert [a.status for a in restored.snapshot.actions] == ["succeeded", "failed"]
    assert restored.snapshot.budget == state.snapshot.budget
    assert len(restored.messages) == len(state.messages) + 2


def test_terminal_reconciliation_only_appends_audit(agents):
    run, task, state = wait_unknown(agents)
    agents.store.cancel(run)
    with agents.store.transaction() as db:
        before = db.scalar(select(AgentSessionRow)).snapshot
    request = ReconciliationDecision.model_validate(
        {**reconcile(state).model_dump(), "expected_revision": before["revision"]}
    )
    audit = agents.reconcile_action("session", "action-0", request)
    assert not audit.applied
    with agents.store.transaction() as db:
        assert db.scalar(select(AgentSessionRow)).snapshot == before
    assert agents.store.claim() is None


@pytest.mark.parametrize("protected", [False, True])
def test_missing_version_cannot_dispatch_even_through_plain_commit(agents, protected):
    run, task, state = batch(agents, protected=protected)
    with agents.store.transaction() as db:
        db.execute(update(AgentActionRow).values(tool_binding=None))
    before = agents.load(task)
    with pytest.raises(StateConflict):
        agents.commit_transition(
            task,
            AgentTransition(
                snapshot=candidate(
                    before.snapshot,
                    actions=(before.snapshot.actions[0].transition("inflight"),),
                    budget=before.snapshot.budget.consume(tool_calls=1),
                )
            ),
        )
    assert agents.load(task) == before


def test_approval_for_other_action_cannot_authorize_dispatch(agents):
    run, task, state = batch(agents, count=2)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)
    approval = state.approvals[0]
    agents.decide_action(approval.approval_id, decision(state))
    new = agents.store.claim(300)
    state = agents.load(new)
    with pytest.raises(StateConflict):
        agents.commit_transition(
            new,
            AgentTransition(
                snapshot=candidate(
                    state.snapshot,
                    actions=(
                        state.snapshot.actions[0],
                        state.snapshot.actions[1].transition("inflight"),
                    ),
                    budget=state.snapshot.budget.consume(tool_calls=1),
                ),
                authorizations=(approval.approval_id,),
            ),
        )
    assert agents.load(new) == state


def test_only_one_pending_request_and_restart_preserves_review(agents):
    run, task, state = batch(agents, count=2)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)
    with pytest.raises(LostLease):
        agents.request_action_approval(task, "action-1", state.snapshot.revision)
    agents.store.engine.dispose()
    assert (
        SQLAgentStore(agents.store).get_action_approval(state.approvals[0].approval_id)
        == state.approvals[0]
    )
    with agents.store.transaction() as db:
        assert len(list(db.scalars(select(AgentActionApprovalRow)))) == 1


@pytest.mark.parametrize("same", [False, True])
def test_concurrent_operators_make_only_one_approval_transition(agents, same):
    run, task, state = batch(agents)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)
    barrier = Barrier(2)

    def decide(index):
        barrier.wait()
        try:
            return agents.decide_action(
                state.approvals[0].approval_id,
                ApprovalDecision(
                    expected_revision=state.snapshot.revision,
                    decision="approved",
                    actor="operator" if same else f"operator-{index}",
                ),
            )
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, range(2)))
    assert sum(r is not None for r in results) == (2 if same else 1)
    assert agents.review_history("session")["revision"] == state.snapshot.revision + 1


@pytest.mark.parametrize(
    "race", ["approval_cancel", "reconcile_cancel", "reconcile_claim", "two_reconciles"]
)
def test_operator_cancel_claim_races_preserve_cas_and_terminal_rules(agents, race):
    if race == "approval_cancel":
        run, task, state = batch(agents)
        state = agents.request_action_approval(
            task, "action-0", state.snapshot.revision
        )
    else:
        run, task, state = wait_unknown(agents)
    barrier = Barrier(2)

    def operate(index):
        barrier.wait()
        try:
            if index == 0 or race == "two_reconciles":
                if race == "approval_cancel":
                    return agents.decide_action(
                        state.approvals[0].approval_id, decision(state)
                    )
                return agents.reconcile_action(
                    "session",
                    "action-0",
                    reconcile(state, request_id=f"request-{index}"),
                )
            if race == "reconcile_claim":
                return agents.store.claim(300)
            agents.store.cancel(run)
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operate, range(2)))
    with agents.store.transaction() as db:
        saved = AgentSnapshot.model_validate(
            db.scalar(select(AgentSessionRow)).snapshot
        )
        audits = list(db.scalars(select(AgentActionReconciliationRow)))
    if "cancel" in race:
        assert (
            saved.status == "cancelled"
            and agents.store.get_run(run)["status"] == "cancelled"
        )
        assert agents.store.claim() is None
    else:
        assert saved.revision == state.snapshot.revision + 1
        assert saved.status == "actions_pending" and len(audits) == 1
    if race == "two_reconciles":
        assert sum(r is not None for r in results) == 1


def test_operator_failure_rolls_back_approval_and_snapshot(agents):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    run, task, state = batch(agents)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)

    def fail_event(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO EVENTS"):
            raise IntegrityError("safe failure", None, None)

    event.listen(agents.store.engine, "before_cursor_execute", fail_event)
    try:
        with pytest.raises(StateConflict):
            agents.decide_action(state.approvals[0].approval_id, decision(state))
    finally:
        event.remove(agents.store.engine, "before_cursor_execute", fail_event)
    assert (
        agents.get_action_approval(state.approvals[0].approval_id).decision == "pending"
    )
    assert agents.review_history("session")["revision"] == state.snapshot.revision


def test_dispatch_failure_rolls_back_authorization_consumption(agents):
    run, task, state = batch(agents)
    state = agents.request_action_approval(task, "action-0", state.snapshot.revision)
    approval = state.approvals[0]
    agents.decide_action(approval.approval_id, decision(state))
    task = agents.store.claim(300)
    state = agents.load(task)
    with pytest.raises(StateConflict):
        agents.commit_transition(
            task,
            AgentTransition(
                snapshot=candidate(
                    state.snapshot,
                    actions=(state.snapshot.actions[0].transition("inflight"),),
                    budget=state.snapshot.budget.consume(tool_calls=1),
                ),
                authorizations=(approval.approval_id,),
                messages=(
                    AgentMessage(
                        sequence=len(state.messages) + 1, role="user", payload={}
                    ),
                ),
            ),
        )
    assert agents.load(task) == state
    assert agents.get_action_approval(approval.approval_id).consumed_at is None


@pytest.mark.parametrize("terminal", ["failed", "exhausted"])
def test_failed_terminal_reconciliation_preserves_action_and_observation(
    agents, terminal
):
    run, task, state = batch(agents, protected=False)
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(
                state.snapshot,
                actions=(state.snapshot.actions[0].transition("inflight"),),
                budget=state.snapshot.budget.consume(tool_calls=1),
            )
        ),
    )
    state = agents.fail_agent_step(
        task,
        AgentTransition(
            snapshot=candidate(
                state.snapshot,
                status=terminal,
                actions=(
                    state.snapshot.actions[0].transition(
                        "unknown", error_code="Interrupted"
                    ),
                ),
            ),
            observations=(
                ActionObservation(
                    sequence=len(state.messages), action_id="action-0", result=None
                ),
            ),
            error_code="Interrupted",
        ),
    )
    for i in range(2):
        record = agents.reconcile_action(
            "session", "action-0", reconcile(state, request_id=f"evidence-{i}")
        )
        assert not record.applied and record.previous_status == "unknown"
    with agents.store.transaction() as db:
        restored = agents._read(db, db.scalar(select(AgentSessionRow)))
    assert restored.snapshot == state.snapshot
    assert restored.messages == state.messages and restored.actions == state.actions
    assert len(restored.reconciliations) == 2
    assert agents.store.get_run(run)["status"] == "failed"
    assert agents.store.claim() is None


def test_generic_transition_cannot_resolve_unknown_without_operator_audit(agents):
    run, task, state = wait_unknown(agents)
    from agentflow.infrastructure.persistence.models import StepRow

    with agents.store.transaction() as db:
        db.execute(update(StepRow).values(status="ready"))
    new = agents.store.claim(300)
    with pytest.raises(StateConflict):
        agents.commit_transition(
            new,
            AgentTransition(
                snapshot=candidate(
                    state.snapshot,
                    status="actions_pending",
                    actions=(state.snapshot.actions[0].reconcile("succeeded"),),
                ),
                observations=(
                    ActionObservation(
                        sequence=len(state.messages), action_id="action-0", result=None
                    ),
                ),
            ),
        )
