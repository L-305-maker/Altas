from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_summary_consumption import consumer_fixture, consumer_worker, read_summary

from agentflow.application.agents.summary import SummarySourceError
from agentflow.application.ports.agent_store import (
    AgentDataError,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.models import (
    AgentMessageRow,
    AgentModelAttemptRow,
    AgentSessionRow,
    ArtifactRow,
    RunRow,
    StepRow,
)
from agentflow.infrastructure.persistence.store import json_bytes


@pytest.mark.asyncio
async def test_consumption_does_not_trust_task_dependency_or_inputs(store):
    run, agents = await consumer_fixture(store)
    task = store.claim(300)
    plan = agents.summary_consumption(task)
    assert (
        agents.summary_consumption(
            replace(task, inputs={"summary_consumption": {"allow_claim_text": True}})
        )
        == plan
    )
    assert all(c.text is None for c in plan.view.selected_claims)
    with pytest.raises(SummarySourceError):
        agents.summary_consumption(
            replace(
                task,
                dependencies={
                    "summary": {
                        "summary_version": 1,
                        "assurance": "generated_not_fact_verified",
                    }
                },
            )
        )
    with pytest.raises(LostLease):
        agents.summary_consumption(replace(task, token="old"))
    with store.transaction() as db:
        row = db.get(RunRow, run)
        spec = deepcopy(row.spec)
        spec["steps"][-1]["depends_on"] = ["source"]
        row.spec = spec
    with pytest.raises(SummarySourceError):
        agents.summary_consumption(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "missing_artifact",
        "other_run",
        "artifact_text",
        "artifact_reference",
        "artifact_digest",
        "missing_attempt",
        "failed_attempt",
        "failed_summary",
        "cancelled_summary",
        "source_revision",
        "source_history",
        "summary_response",
    ],
)
async def test_consumption_verifies_real_artifact_execution_and_source(store, damage):
    run, agents = await consumer_fixture(store)
    task = store.claim(300)
    assert agents.summary_consumption(task)
    other = (
        store.submit(
            store.register(
                WorkflowSpec(
                    name="other-run", steps=(StepSpec(id="plain", handler="plain"),)
                )
            ),
            {},
        )
        if damage == "other_run"
        else None
    )
    with store.transaction() as db:
        step = db.scalar(
            select(StepRow).where(StepRow.run_id == run, StepRow.key == "summary")
        )
        row = db.scalar(
            select(AgentSessionRow).where(AgentSessionRow.step_row_id == step.id)
        )
        artifact = db.scalar(select(ArtifactRow).where(ArtifactRow.step_id == step.id))
        attempt = db.scalar(
            select(AgentModelAttemptRow)
            .where(AgentModelAttemptRow.session_id == row.id)
            .order_by(AgentModelAttemptRow.created_at.desc())
        )
        if damage == "missing_artifact":
            db.delete(artifact)
        elif damage == "other_run":
            artifact.run_id = other
        elif damage.startswith("artifact_"):
            data = deepcopy(artifact.content)
            if damage == "artifact_text":
                data["claims"][0]["text"] = "forged derived instruction"
            elif damage == "artifact_reference":
                data["claims"][0]["evidence_references"] = ["f" * 64]
            if damage != "artifact_digest":
                artifact.content, artifact.digest, step.output = (
                    data,
                    sha256(json_bytes(data)).hexdigest(),
                    data,
                )
                task = replace(task, dependencies={"summary": data})
            else:
                artifact.digest = "f" * 64
        elif damage == "missing_attempt":
            db.delete(attempt)
        elif damage == "failed_attempt":
            attempt.outcome = "failed"
        elif damage.endswith("_summary"):
            step.status = damage.split("_")[0]
        elif damage in {"source_revision", "source_history"}:
            source_step = db.scalar(
                select(StepRow).where(StepRow.run_id == run, StepRow.key == "source")
            )
            source = db.scalar(
                select(AgentSessionRow).where(
                    AgentSessionRow.step_row_id == source_step.id
                )
            )
            if damage == "source_revision":
                snapshot = deepcopy(source.snapshot)
                snapshot["revision"] += 1
                source.snapshot, source.revision = snapshot, source.revision + 1
            else:
                db.delete(
                    db.scalar(
                        select(AgentMessageRow).where(
                            AgentMessageRow.session_id == source.id,
                            AgentMessageRow.role == "assistant",
                        )
                    )
                )
        else:
            message = db.scalar(
                select(AgentMessageRow)
                .where(
                    AgentMessageRow.session_id == row.id,
                    AgentMessageRow.role == "assistant",
                )
                .order_by(AgentMessageRow.sequence.desc())
            )
            data = deepcopy(message.payload)
            data["message"]["content"] = "{}"
            message.payload = data
    with pytest.raises((StateConflict, ValueError)):
        agents.summary_consumption(task)


@pytest.mark.asyncio
async def test_consumption_retry_reuses_frozen_view_and_completed_action(
    store, runtime_invariants, advance_database_clock
):
    run, agents = await consumer_fixture(store, allow_claim_text=True)
    provider = MockProvider([read_summary(), ModelRetryableError()])
    assert await consumer_worker(store, agents, provider).tick()
    before = runtime_invariants(store, run)["next"].agent
    assert (
        before.snapshot.status == "ready_model"
        and before.model_attempts[-1].outcome == "failed"
    )
    advance_database_clock(2)
    provider2 = MockProvider([final("done")])
    assert await consumer_worker(store, agents, provider2).tick()
    after = runtime_invariants(store, run)["next"].agent
    assert after.snapshot.status == "succeeded"
    assert (
        after.model_attempts[-1].model_call_id
        == before.model_attempts[-1].model_call_id
    )
    assert (
        after.model_attempts[-1].context_projection
        == before.model_attempts[-1].context_projection
    )
    assert after.actions == before.actions and after.snapshot.budget.model_attempts == 3


@pytest.mark.asyncio
async def test_consumption_policy_change_stops_retry_and_does_not_block_cancel(
    store, runtime_invariants, advance_database_clock
):
    run, agents = await consumer_fixture(store)
    await consumer_worker(store, agents, MockProvider([ModelRetryableError()])).tick()
    before = runtime_invariants(store, run)["next"].agent
    with store.transaction() as db:
        row = db.get(RunRow, run)
        data = deepcopy(row.spec)
        data["steps"][-1]["parameters"]["agent"]["summary_consumption"][
            "allow_claim_text"
        ] = True
        row.spec = data
    advance_database_clock(2)
    provider = MockProvider([final("unsafe")])
    await consumer_worker(store, agents, provider).tick()
    assert provider.calls == 0
    store.cancel(run)
    with agents._operator_transaction(before.snapshot.id) as (_, _, step, _, restored):
        assert step.status == "cancelled" and restored.snapshot.status == "cancelled"
        assert restored.model_attempts == before.model_attempts


@pytest.mark.asyncio
async def test_unknown_consumption_action_never_automatically_rereads(
    store, runtime_invariants, monkeypatch
):
    run, agents = await consumer_fixture(store)
    original = agents.commit_transition

    def fail_result(task, transition):
        if transition.observations and any(
            a.status == "succeeded" for a in transition.snapshot.actions
        ):
            raise AgentDataError("SyntheticUnknown")
        return original(task, transition)

    with monkeypatch.context() as patch:
        patch.setattr(agents, "commit_transition", fail_result)
        await consumer_worker(store, agents, MockProvider([read_summary()])).tick()
    waiting = runtime_invariants(store, run)["next"].agent
    assert waiting.snapshot.status == "waiting_reconciliation"
    assert waiting.actions[0].action.status == "unknown"
    provider = MockProvider([final("unsafe")])
    assert not await consumer_worker(store, agents, provider).tick()
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_consumer_byte_limit_fails_before_provider(store):
    run, agents = await consumer_fixture(store, max_consumption_bytes=256)
    provider = MockProvider([final("unsafe")])
    await consumer_worker(store, agents, provider).tick()
    assert provider.calls == 0 and len(store.artifacts(run)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", ["succeeded", "failed"])
async def test_reconciliation_cannot_replace_authorized_consumer_view(
    store, runtime_invariants, monkeypatch, confirmed
):
    from agentflow.domain.agent.review import ReconciliationDecision

    run, agents = await consumer_fixture(store)
    original = agents.commit_transition

    def fail_result(task, transition):
        if transition.observations and any(
            a.status == "succeeded" for a in transition.snapshot.actions
        ):
            raise AgentDataError("SyntheticUnknown")
        return original(task, transition)

    with monkeypatch.context() as patch:
        patch.setattr(agents, "commit_transition", fail_result)
        await consumer_worker(store, agents, MockProvider([read_summary()])).tick()
    waiting = runtime_invariants(store, run)["next"].agent
    agents.reconcile_action(
        waiting.snapshot.id,
        waiting.actions[0].action.id,
        ReconciliationDecision(
            request_id="consumer-false-result",
            expected_revision=waiting.snapshot.revision,
            confirmed_status=confirmed,
            error_code="SyntheticFailed" if confirmed == "failed" else None,
            result={"private": "SYNTHETIC_UNAUTHORIZED_CONSUMER_RESULT"},
            actor="test",
            evidence_reference="synthetic",
        ),
    )
    provider = MockProvider([final("unsafe")])
    await consumer_worker(store, agents, provider).tick()
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_source_history_database_failure_cannot_create_consumer_attempt(store):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    from agentflow.application.ports.agent_store import AgentStoreUnavailable

    run, agents = await consumer_fixture(store)
    task = store.claim(300)

    def fail_read(conn, cursor, statement, parameters, context, executemany):
        if "FROM agent_messages" in statement:
            raise OperationalError("SyntheticReadFailure", None, None)

    event.listen(store.engine, "before_cursor_execute", fail_read)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.summary_consumption(task)
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_read)
    with store.transaction() as db:
        step = db.scalar(
            select(StepRow).where(StepRow.run_id == run, StepRow.key == "next")
        )
        assert (
            db.scalar(
                select(AgentSessionRow).where(AgentSessionRow.step_row_id == step.id)
            )
            is None
        )
