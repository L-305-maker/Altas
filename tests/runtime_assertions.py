"""测试专用的只读持久化契约断言，不补写或修复被检查的状态。"""

import math
from dataclasses import dataclass

import pytest
from sqlalchemy import select

from agentflow.application.ports.agent_store import (
    AgentSession,
    AgentTransition,
    LostLease,
)
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentSessionRow,
    ArtifactRow,
    StepRow,
)


@dataclass(frozen=True)
class Checkpoint:
    agent: AgentSession
    step: dict
    run_status: str
    continuation: bool
    artifacts: tuple


class RuntimeInvariants:
    def __init__(self):
        self.previous = {}

    def __call__(self, store, run_id):
        result = {}
        with store.transaction() as db:
            run = store._locked_run(db, run_id)
            specs = {s.id: s for s in WorkflowSpec.model_validate(run.spec).steps}
            steps = db.scalars(
                select(StepRow)
                .where(StepRow.run_id == run_id)
                .order_by(StepRow.id)
                .with_for_update()
            )
            for step in steps:
                row = db.scalar(
                    select(AgentSessionRow)
                    .where(AgentSessionRow.step_row_id == step.id)
                    .with_for_update()
                )
                if row is None:
                    continue
                state = SQLAgentStore(store)._read(db, row)
                if state.observed_usage is not None:
                    assert state.observed_usage.summary.attempts == len(
                        state.model_attempts
                    )
                    if state.snapshot.status == "exhausted" and step.error in {
                        "AgentObservedUsageLimit",
                        "AgentUsageUnavailable",
                    }:
                        assert state.observed_usage.blocked
                artifacts = tuple(
                    a.content
                    for a in db.scalars(
                        select(ArtifactRow).where(ArtifactRow.step_id == step.id)
                    )
                )
                saved = Checkpoint(
                    state,
                    {
                        "status": step.status,
                        "attempts": step.attempts,
                        "token": step.token,
                        "lease_until": step.lease_until,
                        "available_at": step.available_at,
                        "error": step.error,
                    },
                    run.status,
                    row.continuation_pending,
                    artifacts,
                )
                snapshot = state.snapshot
                expected = {
                    "succeeded": "succeeded",
                    "failed": "failed",
                    "exhausted": "failed",
                    "cancelled": "cancelled",
                }
                if snapshot.status.is_terminal:
                    assert step.status == expected[snapshot.status.value]
                if step.status in {"succeeded", "failed", "cancelled"}:
                    assert snapshot.status.is_terminal
                if step.status == "waiting":
                    assert snapshot.status in {
                        "waiting_approval",
                        "waiting_reconciliation",
                    }
                if snapshot.status in {"waiting_approval", "waiting_reconciliation"}:
                    assert step.status == "waiting"
                    assert not any(a.status == "inflight" for a in snapshot.actions)
                if step.status != "running":
                    assert step.token is None and step.lease_until is None
                if step.status == "ready":
                    # A reconciled crashed lease is a real bounded Step retry.
                    # _ready_review clears its error but keeps continuation false.
                    reconciled_crash_retry = (
                        snapshot.status == "actions_pending"
                        and step.attempts < specs[step.key].max_attempts
                        and any(
                            r.applied
                            and r.action_id in {a.id for a in snapshot.actions}
                            and any(
                                m.role == "tool"
                                and m.payload.get("action_id") == r.action_id
                                and m.payload.get("status") == "unknown"
                                and m.payload.get("error_code") == "LeaseExpired"
                                for m in state.messages
                            )
                            for r in state.reconciliations
                        )
                    )
                    assert (
                        saved.continuation
                        or step.error == "LeaseExpired"
                        or reconciled_crash_retry
                    )
                    if step.error == "ModelRetryable":
                        assert snapshot.status == "ready_model"
                        assert state.model_calls[-1].status == "failed"
                        assert saved.continuation and math.isfinite(step.available_at)
                    if step.error == "ProviderAdmissionDeferred":
                        assert snapshot.status == "ready_model"
                        assert state.provider_key is not None and saved.continuation
                        assert math.isfinite(step.available_at)
                assert len(artifacts) == int(
                    step.status == "succeeded" and specs[step.key].artifact
                )
                assert len(state.model_calls) == snapshot.budget.model_rounds
                assert len(state.model_attempts) == snapshot.budget.model_attempts
                permit_attempts = {
                    (p.model_call_id, p.attempt_index) for p in state.model_permits
                }
                assert len(permit_attempts) == len(state.model_permits)
                assert permit_attempts <= {
                    (a.model_call_id, a.attempt_index) for a in state.model_attempts
                }
                assert [c.round_index for c in state.model_calls] == list(
                    range(1, len(state.model_calls) + 1)
                )
                assert len({c.model_call_id for c in state.model_calls}) == len(
                    state.model_calls
                )
                key = (run_id, step.key)
                old = self.previous.get(key)
                if old:
                    if old.agent.observed_usage is not None:
                        assert state.observed_usage is not None
                        assert (
                            state.observed_usage.policy
                            == old.agent.observed_usage.policy
                        )
                    prior = old.agent.snapshot
                    assert snapshot.revision >= prior.revision
                    if snapshot.revision == prior.revision or prior.status.is_terminal:
                        assert snapshot == prior
                    for counter in (
                        "model_rounds",
                        "model_attempts",
                        "tool_calls",
                        "resource_units",
                    ):
                        assert (
                            getattr(prior.budget, counter)
                            <= getattr(snapshot.budget, counter)
                            <= getattr(snapshot.budget, f"max_{counter}")
                        )
                        assert getattr(prior.budget, f"max_{counter}") == getattr(
                            snapshot.budget, f"max_{counter}"
                        )
                    assert (
                        state.messages[: len(old.agent.messages)] == old.agent.messages
                    )
                    calls = {c.model_call_id: c for c in state.model_calls}
                    attempts = {
                        (a.model_call_id, a.attempt_index): a
                        for a in state.model_attempts
                    }
                    for attempt in old.agent.model_attempts:
                        current = attempts[
                            (attempt.model_call_id, attempt.attempt_index)
                        ]
                        assert current.created_at == attempt.created_at
                        assert current.accounting == attempt.accounting
                        assert current.context_projection == attempt.context_projection
                        assert current.request_contract == attempt.request_contract
                        if attempt.outcome != "inflight":
                            assert current == attempt
                    permits = {p.id: p for p in state.model_permits}
                    for permit in old.agent.model_permits:
                        current = permits[permit.id]
                        assert current.model_dump(
                            exclude={"status", "released_at"}
                        ) == permit.model_dump(exclude={"status", "released_at"})
                        if permit.status != "active":
                            assert current == permit
                    for call in old.agent.model_calls:
                        assert calls[call.model_call_id].round_index == call.round_index
                        assert calls[call.model_call_id].attempts >= call.attempts
                    actions = {a.action.id: a for a in state.actions}
                    for stored in old.agent.actions:
                        current = actions[stored.action.id]
                        assert current.idempotency_key == stored.idempotency_key
                        assert (
                            current.arguments == stored.arguments
                            and current.tool_binding == stored.tool_binding
                        )
                        assert (
                            current.action.model_call_id == stored.action.model_call_id
                        )
                        assert current.call_index == stored.call_index
                        for field in (
                            "id",
                            "agent_id",
                            "model_call_id",
                            "tool_name",
                            "arguments_digest",
                            "side_effect",
                        ):
                            assert getattr(current.action, field) == getattr(
                                stored.action, field
                            )
                        if stored.action.status.is_terminal:
                            assert current == stored
                        if stored.action.status == "unknown":
                            assert current.action.status in {
                                "unknown",
                                "succeeded",
                                "failed",
                            }
                            if current.action.status != "unknown":
                                assert any(
                                    r.action_id == stored.action.id and r.applied
                                    for r in state.reconciliations
                                )
                    if old.run_status == "cancelled":
                        assert (
                            saved.run_status == "cancelled" and saved.step == old.step
                        )
                result[step.key] = saved
        for key, value in result.items():
            self.previous[(run_id, key)] = value
        return result

    def fenced(self, agents, task):
        with pytest.raises(LostLease):
            agents.load(task)
        current = self(agents.store, task.run_id)[task.step_id].agent.snapshot
        candidate = AgentSnapshot.model_validate(
            {**current.model_dump(), "revision": current.revision + 1}
        )
        with pytest.raises(LostLease):
            agents.commit_transition(task, AgentTransition(snapshot=candidate))


@pytest.fixture
def runtime_invariants():
    return RuntimeInvariants()
