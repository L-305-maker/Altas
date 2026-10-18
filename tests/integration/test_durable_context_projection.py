import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_model_governance import due, saved, submit, worker
from test_observed_usage_governance import reported, usage_policy
from test_provider_admission import opened, policy, reservation
from test_provider_admission import worker as admitted_worker

from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.agents.durable_runner import DurableAgentRunner, successor
from agentflow.application.agents.projection import digest_context, project_context
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionPolicy,
    ContextProjectionReservation,
)
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import AgentModelAttemptRow, RunRow
from agentflow.infrastructure.persistence.store import Store


def projected_reservation(state, schemas=None):
    schemas = schemas or []
    projected = project_context(
        AgentContextBuilder().build(state),
        schemas,
        ContextProjectionPolicy(),
        1_000_000,
    )
    return AgentTransition.model_validate(
        {
            **reservation(state).model_dump(),
            "context_projection": ContextProjectionReservation(
                manifest=projected.manifest, tools=tuple(schemas)
            ),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected", [("full", "exhausted"), ("recent_complete_rounds", "succeeded")]
)
async def test_projection_continues_long_history_and_preserves_audit(
    store, runtime_invariants, mode, expected
):
    provider = MockProvider([proposal(), proposal(), final()])
    seen = []
    respond = provider.respond

    async def capture(messages, schemas):
        seen.append(messages)
        return await respond(messages, schemas)

    provider.respond = capture

    async def invoke(_):
        return {"text": "旧结果" * 80}

    runtime = tools(invoke)
    run = submit(
        store,
        max_context_bytes=1500,
        context_projection={"mode": mode},
    )
    assert await worker(store, provider, runtime=runtime).tick()
    step, agent = saved(store, run)
    assert agent.snapshot["status"] == expected
    state = runtime_invariants(store, run)["work"].agent
    assert len(state.actions) == 2
    assert all(a.result == {"text": "旧结果" * 80} for a in state.actions)
    assert len([m for m in state.messages if m.role == "tool"]) == 2
    assert len(state.messages) == (7 if mode == "recent_complete_rounds" else 6)
    assert len(store.artifacts(run)) == int(expected == "succeeded")
    with store.transaction() as db:
        attempts = list(
            db.scalars(
                select(AgentModelAttemptRow)
                .where(AgentModelAttemptRow.session_id == agent.id)
                .order_by(AgentModelAttemptRow.created_at)
            )
        )
    if mode == "full":
        assert step.error == "AgentContextLimit" and provider.calls == 2
        assert len(attempts) == 2
        return
    assert len(attempts) == 3
    assert attempts[-1].context_projection["omitted_round_count"] == 1
    assert len([m for m in seen[-1] if m["role"] == "tool"]) == 1
    assert agent.snapshot["budget"]["model_attempts"] == 3


@pytest.mark.asyncio
async def test_retry_restores_same_projection_and_logical_call(store):
    run = submit(store, context_projection={"mode": "recent_complete_rounds"})
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider).tick()
    assert provider.calls == 1
    due(store, run)
    store.engine.dispose()
    assert await worker(store, provider).tick()
    step, agent = saved(store, run)
    with store.transaction() as db:
        attempts = list(
            db.scalars(
                select(AgentModelAttemptRow)
                .where(AgentModelAttemptRow.session_id == agent.id)
                .order_by(AgentModelAttemptRow.attempt_index)
            )
        )
    assert step.status == "succeeded"
    assert attempts[0].model_call_id == attempts[1].model_call_id
    assert attempts[0].context_projection == attempts[1].context_projection
    assert attempts[0].context_projection is not None


@pytest.mark.asyncio
async def test_reserved_manifest_describes_the_actual_immutable_schema_payload(store):
    agents = SQLAgentStore(store)
    run = submit(store)

    async def invoke(_):
        pytest.fail("final response does not dispatch tools")

    runtime = tools(invoke)
    original = agents.commit_transition
    manifests = []

    def hot_reload(task, transition):
        state = original(task, transition)
        if transition.context_projection:
            manifests.append(transition.context_projection.manifest)
            runtime._tools["echo"].schema["description"] = "reloaded after reservation"
        return state

    agents.commit_transition = hot_reload
    provider = MockProvider([final()])
    respond = provider.respond

    async def capture(messages, schemas):
        assert (
            digest_context({"messages": messages, "tools": schemas})
            == manifests[0].projected_context_digest
        )
        return await respond(messages, schemas)

    provider.respond = capture
    assert await worker(store, provider, agents=agents, runtime=runtime).tick()
    assert saved(store, run)[0].status == "succeeded"


@pytest.mark.asyncio
async def test_transient_schema_is_not_charged_as_persisted_transition_data(store):
    run = submit(store, max_context_bytes=3_000_000)

    async def invoke(_):
        pytest.fail("final response does not dispatch")

    runtime = tools(invoke)
    runtime._tools["echo"] = replace(
        runtime._tools["echo"], description="x" * 2_000_001
    )
    provider = MockProvider([final()])
    assert await worker(store, provider, runtime=runtime).tick()
    assert saved(store, run)[0].status == "succeeded" and provider.calls == 1


def test_manifest_capacity_keeps_room_for_unknown_and_cancellation(store, monkeypatch):
    from test_action_reviews import fill_to_admission_limit

    import agentflow.infrastructure.persistence.agent_store as module

    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 120_000)
    agents = SQLAgentStore(store)
    task, ready = opened(agents)
    state = agents.commit_transition(task, projected_reservation(ready))
    state = fill_to_admission_limit(agents, task, state)
    manifest = state.model_attempts[0].context_projection
    store.cancel(task.run_id)
    with agents._operator_transaction(state.snapshot.id) as (_, _, step, _, restored):
        assert step.status == "cancelled" and restored.snapshot.status == "cancelled"
        assert restored.model_attempts[0].outcome == "unknown"
        assert restored.model_attempts[0].context_projection == manifest
        assert restored.messages == state.messages


@pytest.mark.asyncio
async def test_retry_schema_drift_fails_before_permit_and_budget(
    store, runtime_invariants
):
    config = policy()
    run = submit(store, context_projection={"mode": "recent_complete_rounds"})
    provider = MockProvider([ModelRetryableError(), final()])

    async def invoke(_):
        pytest.fail("no tool dispatch")

    assert await admitted_worker(
        store, provider, config, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    # A new worker deployment changed the advertised schema/description.
    runtime = tools(invoke)
    runtime._tools["echo"] = replace(runtime._tools["echo"], description="changed")
    assert await admitted_worker(
        store, provider, config, runtime=runtime, allowed={"echo"}
    ).tick()
    after = runtime_invariants(store, run)["work"]
    assert after.step["error"] == "AgentContextProjectionChanged"
    assert after.agent.snapshot.status == "failed" and provider.calls == 1
    assert after.agent.model_attempts == before.model_attempts
    assert after.agent.model_permits == before.model_permits
    assert after.agent.snapshot.budget == before.snapshot.budget


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["context", "usage", "admission"])
async def test_projection_respects_all_preflight_gates(
    store, runtime_invariants, blocked
):
    config = policy()
    agents = SQLAgentStore(store, admission_policy=config)
    options = {"context_projection": {"mode": "recent_complete_rounds"}}
    if blocked == "context":
        options["max_context_bytes"] = 1
    elif blocked == "usage":
        options["observed_usage"] = usage_policy(
            max_observed_total_tokens=1
        ).model_dump()
    else:
        task, initial = opened(agents)
        agents.reserve_model_attempt(task, reservation(initial))
    run = submit(store, **options)
    provider = MockProvider([reported(proposal(), {"total_tokens": 1}), final()])

    async def invoke(_):
        return {"value": 1}

    assert await admitted_worker(
        store, provider, config, agents=agents, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"]
    if blocked == "usage":
        assert provider.calls == 1 and len(state.agent.model_permits) == 1
        assert state.step["error"] == "AgentObservedUsageLimit"
    else:
        assert provider.calls == 0
        assert state.agent.model_attempts == state.agent.model_permits == ()
        assert state.agent.snapshot.budget.model_attempts == 0
        assert state.step["error"] == (
            "AgentContextLimit" if blocked == "context" else "ProviderAdmissionDeferred"
        )


@pytest.mark.asyncio
async def test_approval_reconciliation_replays_only_final_result_with_projection(
    store, runtime_invariants
):
    agents = SQLAgentStore(store)
    run = submit(store, context_projection={"mode": "recent_complete_rounds"})
    invoked = []

    async def invoke(args):
        invoked.append(args)
        raise TimeoutError

    runtime = tools(invoke, requires_approval=True)
    provider = MockProvider([proposal(), final()])
    executor = worker(store, provider, agents=agents, runtime=runtime)
    assert await executor.tick()
    pending = runtime_invariants(store, run)["work"].agent
    agents.decide_action(
        pending.approvals[0].approval_id,
        ApprovalDecision(
            expected_revision=pending.snapshot.revision,
            decision="approved",
            actor="test",
        ),
    )
    assert await executor.tick()
    waiting = runtime_invariants(store, run)["work"].agent
    assert waiting.snapshot.status == "waiting_reconciliation" and provider.calls == 1
    assert not await executor.tick()
    agents.reconcile_action(
        waiting.snapshot.id,
        waiting.actions[0].action.id,
        ReconciliationDecision(
            request_id="projected-reconcile",
            expected_revision=waiting.snapshot.revision,
            confirmed_status="succeeded",
            result={"value": "confirmed"},
            evidence_reference="test",
            actor="test",
        ),
    )
    assert await executor.tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded" and len(invoked) == 1
    assert len([m for m in state.messages if m.role == "tool"]) == 2
    observations = [m for m in provider.contexts[-1] if m["role"] == "tool"]
    assert (
        len(observations) == 1 and '"status":"succeeded"' in observations[0]["content"]
    )
    assert (
        state.approvals[0].consumed_at is not None and len(state.reconciliations) == 1
    )


def test_manifest_recomputed_before_admission_and_lease_checked_first(
    store, monkeypatch
):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    proposed = projected_reservation(state)
    bad = proposed.model_dump()
    bad["context_projection"]["manifest"]["full_context_digest"] = "0" * 64
    bad = AgentTransition.model_validate(bad)

    def forbidden(*_):
        pytest.fail("invalid context cannot acquire a provider permit")

    monkeypatch.setattr(agents.admission, "acquire", forbidden)
    with pytest.raises(ContextProjectionError):
        agents.reserve_model_attempt(task, bad)
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(replace(task, token="stale"), proposed)
    assert agents.load(task) == state


def test_attempt_manifest_and_revision_roll_back_together(store):
    agents = SQLAgentStore(store)
    task, state = opened(agents)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO agent_model_attempts"):
            raise OperationalError("safe injected failure", None, None)

    event.listen(store.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.commit_transition(task, projected_reservation(state))
    finally:
        event.remove(store.engine, "before_cursor_execute", fail)
    assert agents.load(task) == state


def test_retry_cannot_drop_manifest_or_override_persisted_policy(store):
    agents = SQLAgentStore(store)
    task, state = opened(agents)
    proposed = projected_reservation(state)
    changed = proposed.model_dump()
    changed["context_projection"]["manifest"]["selection_policy"] = (
        "recent_complete_rounds"
    )
    with pytest.raises(ContextProjectionError):
        agents.commit_transition(task, AgentTransition.model_validate(changed))
    assert agents.load(task) == state
    pending = agents.commit_transition(task, proposed)
    failed = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=successor(pending.snapshot, status="ready_model"),
            model_result=ModelCallResult(
                model_call_id=pending.snapshot.model_call_id, status="failed"
            ),
        ),
    )
    assert (
        failed.model_attempts[0].context_projection
        == proposed.context_projection.manifest
    )
    with pytest.raises(ContextProjectionError):
        agents.commit_transition(task, reservation(failed))
    assert agents.load(task) == failed


@pytest.mark.asyncio
async def test_legacy_failed_attempt_without_manifest_cannot_silently_retry(
    store, runtime_invariants
):
    from sqlalchemy import update

    run = submit(store)
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider).tick()
    budget = saved(store, run)[1].snapshot["budget"]
    # Emulate a real 0006 reservation: its context identity was never persisted.
    with store.transaction() as db:
        db.execute(update(AgentModelAttemptRow).values(context_projection=None))
    due(store, run)
    assert await worker(store, provider).tick()
    after = runtime_invariants(store, run)["work"]
    assert (
        after.step["error"] == "AgentContextProjectionChanged" and provider.calls == 1
    )
    assert len(after.agent.model_attempts) == 1
    assert after.agent.model_attempts[0].context_projection is None
    assert after.agent.snapshot.budget.model_dump() == budget


@pytest.mark.asyncio
async def test_reservation_ack_loss_recovers_unknown_without_resending(
    store, monkeypatch, runtime_invariants
):
    agents = SQLAgentStore(store)
    run = submit(store, context_projection={"mode": "recent_complete_rounds"})
    task = store.claim(300)
    original = agents.commit_transition

    def lost_ack(task, transition):
        original(task, transition)
        raise AgentStoreUnavailable("commit acknowledgement lost")

    monkeypatch.setattr(agents, "commit_transition", lost_ack)
    provider = MockProvider()
    runner = DurableAgentRunner(agents, provider, ToolRuntime())
    with pytest.raises(AgentStoreUnavailable):
        await runner.run(task, "goal", allowed=set())
    before = runtime_invariants(store, run)["work"].agent
    assert (
        len(before.model_attempts) == 1
        and before.model_attempts[0].context_projection is not None
    )
    store.engine.dispose()
    result = await asyncio.wait_for(runner.run(task, "goal", allowed=set()), timeout=10)
    assert result.status == "failed" and provider.calls == 0
    after = runtime_invariants(store, run)["work"].agent
    assert after.model_attempts[0].outcome == "unknown"
    assert (
        after.model_attempts[0].context_projection
        == before.model_attempts[0].context_projection
    )
    assert after.snapshot.budget == before.snapshot.budget


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_independent_postgres_reservations_cas_one_manifest(store, runtime_invariants):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    proposed = projected_reservation(state)
    barrier = Barrier(2, timeout=10)

    def reserve(_):
        other = Store(store.engine.url.render_as_string(hide_password=False))
        try:
            barrier.wait()
            return SQLAgentStore(
                other, admission_policy=policy()
            ).reserve_model_attempt(task, proposed)
        except StateConflict:
            return None
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            f.result(timeout=20) for f in [pool.submit(reserve, i) for i in range(2)]
        ]
    assert sum(o is not None for o in outcomes) == 1
    saved = runtime_invariants(store, task.run_id)["work"].agent
    assert len(saved.model_attempts) == len(saved.model_permits) == 1
    assert (
        saved.model_attempts[0].context_projection
        == proposed.context_projection.manifest
    )


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_skip_locked_empty_tick_does_not_change_projection_or_attempts(
    store, runtime_invariants
):
    run = submit(store, context_projection={"mode": "recent_complete_rounds"})
    provider = MockProvider([final()])
    executor = worker(store, provider)
    async with asyncio.timeout(10):
        with store.transaction() as db:
            db.scalar(select(RunRow).where(RunRow.id == run).with_for_update())
            assert not await executor.tick()
            assert provider.calls == 0
            assert saved(store, run)[0].attempts == 0
        assert await executor.tick()
    state = runtime_invariants(store, run)["work"].agent
    assert len(state.model_attempts) == provider.calls == 1
    assert state.model_attempts[0].context_projection is not None
