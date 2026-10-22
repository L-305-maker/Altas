import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_summary_consumption import consumer_fixture, consumer_worker, read_summary

from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.agents.durable_runner import (
    DurableAgentConfig,
    DurableAgentRunner,
)
from agentflow.application.agents.projection import project_context
from agentflow.application.agents.summary_consumption import consumption_tool
from agentflow.application.ports.agent_store import (
    AgentDataError,
    AgentMessage,
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.context_projection import ContextProjectionReservation
from agentflow.application.ports.model_provider import ModelResponse
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import StepRow
from agentflow.infrastructure.persistence.store import Store


def open_consumer(agents):
    import hashlib

    from test_provider_admission import reservation

    task = agents.store.claim(300)
    plan = agents.summary_consumption(task)
    config_data = dict(task.spec.parameters["agent"])
    config_data.pop("mode")
    config = DurableAgentConfig(**config_data)
    runtime = ToolRuntime()
    runtime.register(consumption_tool(plan.policy))
    messages = DurableAgentRunner(agents, MockProvider(), runtime)._initial_messages(
        task, "consume"
    )
    messages = (
        messages[0],
        AgentMessage(
            sequence=1,
            role="user",
            payload={
                **messages[1].payload,
                "summary_consumption": plan.model_dump(mode="json"),
            },
        ),
    )
    state = agents.open(
        task,
        AgentSnapshot(
            id=hashlib.sha256(task.idempotency_key.encode()).hexdigest(),
            budget=config.initial_budget(),
        ),
        messages=messages,
    )
    schemas = runtime.schemas({"read_verified_summary"})
    projected = project_context(
        AgentContextBuilder().build(state),
        schemas,
        config.context_projection,
        config.max_context_bytes,
        summary_consumption_digest=plan.view.view_digest,
    )
    transition = AgentTransition.model_validate(
        {
            **reservation(state).model_dump(),
            "context_projection": ContextProjectionReservation(
                manifest=projected.manifest, tools=tuple(schemas)
            ),
            "request_contract": MockProvider()
            .prepare_request(projected.messages, schemas, projected.manifest)
            .request.contract,
        }
    )
    return task, state, transition


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["postgres"], indirect=True)
async def test_postgres_consumer_cas_and_stale_worker_have_one_attempt(
    store, runtime_invariants
):
    from test_provider_admission import policy

    run, _ = await consumer_fixture(store)
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state, transition = open_consumer(agents)
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(replace(task, token="old"), transition)
    barrier = Barrier(2, timeout=10)

    def reserve():
        other = Store(store.engine.url.render_as_string(hide_password=False))
        try:
            barrier.wait()
            return SQLAgentStore(
                other, admission_policy=policy()
            ).reserve_model_attempt(task, transition)
        except StateConflict:
            return None
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert sum(r is not None for r in results) == 1
    restored = runtime_invariants(store, run)["next"].agent
    assert len(restored.model_attempts) == len(restored.model_permits) == 1
    assert restored.snapshot.budget.model_attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["reservation", "tool_result", "completion"])
async def test_consumer_commit_ack_loss_uses_only_saved_facts(
    store, runtime_invariants, monkeypatch, advance_database_clock, stage
):
    run, agents = await consumer_fixture(store)
    original = (
        agents.commit_transition
        if stage != "completion"
        else agents.complete_agent_step
    )

    def lose(*args):
        state = original(*args)
        if (
            stage == "completion"
            or (stage == "reservation" and state.snapshot.status == "model_inflight")
            or (
                stage == "tool_result"
                and any(a.action.status == "succeeded" for a in state.actions)
            )
        ):
            raise AgentStoreUnavailable("SyntheticAcknowledgmentLost")
        return state

    provider = MockProvider([read_summary(), final("done")])
    with monkeypatch.context() as patch:
        patch.setattr(
            agents,
            "complete_agent_step" if stage == "completion" else "commit_transition",
            lose,
        )
        await consumer_worker(store, agents, provider).tick()
    before = runtime_invariants(store, run)["next"].agent
    with store.transaction() as db:
        db.execute(
            update(StepRow)
            .where(
                StepRow.run_id == run,
                StepRow.key == "next",
                StepRow.status == "running",
            )
            .values(lease_until=0)
        )
    next_provider = MockProvider([final("done")])
    await consumer_worker(store, agents, next_provider).tick()
    advance_database_clock(2)
    await consumer_worker(store, agents, next_provider).tick()
    after = runtime_invariants(store, run)["next"].agent
    if stage == "reservation":
        assert (
            after.snapshot.status == "failed"
            and after.model_attempts[0].outcome == "unknown"
        )
        assert next_provider.calls == 0
    else:
        assert after.snapshot.status == "succeeded" and after.actions == before.actions
        assert next_provider.calls == (1 if stage == "tool_result" else 0)
        assert len(store.artifacts(run)) == 2  # Consumer is not an Artifact Step.


@pytest.mark.asyncio
async def test_consumer_cancellation_propagates_and_business_cancel_fences(
    store, runtime_invariants
):
    run, agents = await consumer_fixture(store)
    started = asyncio.Event()

    class Waiting(MockProvider):
        async def respond(self, messages, tools=None):
            started.set()
            await asyncio.Event().wait()

    task = store.claim(300)
    job = asyncio.create_task(
        DurableAgentRunner(agents, Waiting(), ToolRuntime()).run(
            task, "consume", allowed=set()
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, 6)
    state = runtime_invariants(store, run)["next"].agent
    assert (
        state.model_attempts[0].outcome == "unknown"
        and state.snapshot.status == "failed"
    )
    with pytest.raises(LostLease):
        agents.load(task)


@pytest.mark.asyncio
async def test_consumer_business_cancel_during_tool_read_cannot_publish_late_result(
    store, runtime_invariants, monkeypatch
):
    run, agents = await consumer_fixture(store)
    original = agents.summary_consumption
    calls = 0

    def cancel_on_read(task):
        nonlocal calls
        calls += 1
        if calls == 2:
            store.cancel(run)
        return original(task)

    monkeypatch.setattr(agents, "summary_consumption", cancel_on_read)
    provider = MockProvider([read_summary(), final("unsafe")])
    await consumer_worker(store, agents, provider).tick()
    state = runtime_invariants(store, run)["next"].agent
    assert (
        state.snapshot.status == "cancelled"
        and state.actions[0].action.status == "unknown"
    )
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_consumer_attempt_insert_failure_rolls_back_identity_and_permit(
    store, runtime_invariants
):
    run, agents = await consumer_fixture(store)
    task, state, transition = open_consumer(agents)

    def reject(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO agent_model_attempts"):
            raise OperationalError("SyntheticDatabaseFailure", None, None)

    event.listen(store.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.commit_transition(task, transition)
    finally:
        event.remove(store.engine, "before_cursor_execute", reject)
    assert agents.load(task) == state
    assert not runtime_invariants(store, run)["next"].agent.model_attempts


@pytest.mark.asyncio
async def test_consumer_observed_usage_blocks_next_model_but_keeps_saved_tool(
    store, runtime_invariants
):
    run, agents = await consumer_fixture(
        store,
        agent_config={
            "observed_usage": {
                "max_observed_total_tokens": 1,
                "missing_usage_policy": "continue_with_warning",
                "policy_version": "1",
            }
        },
    )
    response = ModelResponse(
        assistant_message=read_summary(),
        finish_reason="tool_calls",
        usage={"total_tokens": 1},
    )
    provider = MockProvider([response, final("unsafe")])
    await consumer_worker(store, agents, provider).tick()
    state = runtime_invariants(store, run)["next"].agent
    assert state.snapshot.status == "exhausted" and provider.calls == 1
    assert (
        state.snapshot.budget.model_attempts == 1
        and state.actions[0].action.status == "succeeded"
    )


@pytest.mark.asyncio
async def test_consumer_capacity_preserves_cancel_cleanup_at_real_eight_mb_limit(store):
    run, agents = await consumer_fixture(store)
    task, _, transition = open_consumer(agents)
    pending = agents.commit_transition(task, transition)
    from agentflow.application.agents.durable_runner import successor

    # Stay below the real 2 MB transition cap while filling the real 8 MB
    # session quota in a few batches instead of repeatedly copying tiny tails.
    filled = pending
    for count in (19, 19, 19, 19, 4):
        filled = agents.commit_transition(
            task,
            AgentTransition(
                snapshot=successor(filled.snapshot),
                messages=tuple(
                    AgentMessage(
                        sequence=len(filled.messages) + i,
                        role="user",
                        payload={"padding": "x" * 98_000},
                    )
                    for i in range(count)
                ),
            ),
        )
    assert sum(len(str(m.payload)) for m in filled.messages) > 7_000_000
    with pytest.raises(AgentDataError):
        agents.commit_transition(
            task,
            AgentTransition(
                snapshot=successor(filled.snapshot),
                messages=tuple(
                    AgentMessage(
                        sequence=len(filled.messages) + i,
                        role="user",
                        payload={"padding": "x" * 98_000},
                    )
                    for i in range(2)
                ),
            ),
        )
    assert agents.load(task) == filled
    store.cancel(run)
    with agents._operator_transaction(filled.snapshot.id) as (_, _, step, _, restored):
        assert restored.snapshot.status == step.status == "cancelled"
        assert restored.model_attempts[0].outcome == "unknown"
        assert restored.messages == filled.messages


@pytest.mark.asyncio
async def test_consumer_admission_deferral_does_not_create_attempt(
    store, runtime_invariants
):
    from test_provider_admission import policy

    run, _ = await consumer_fixture(store)
    agents = SQLAgentStore(store, admission_policy=policy())
    task, _, transition = open_consumer(agents)
    assert agents.reserve_model_attempt(task, transition).status == "reserved"
    other_run, _ = await consumer_fixture(store)
    provider = MockProvider([final("unsafe")])
    await consumer_worker(store, agents, provider).tick()
    state = runtime_invariants(store, other_run)["next"]
    assert provider.calls == 0 and not state.agent.model_attempts
    assert state.agent.snapshot.budget.model_attempts == 0
    assert state.step["status"] == "ready" and state.step["token"] is None


@pytest.mark.asyncio
async def test_summary_does_not_authorize_other_consumer_tools(
    store, runtime_invariants
):
    from test_durable_agent_runner import proposal, tools

    from agentflow.application.orchestration.worker import HandlerRegistry, Worker

    run, agents = await consumer_fixture(store, allow_claim_text=True)
    dispatched = []

    async def protected(_):
        dispatched.append(True)
        return {}

    runtime = tools(protected, requires_approval=True)
    handlers = HandlerRegistry()

    async def consume(task):
        return await DurableAgentRunner(
            agents, MockProvider([proposal()]), runtime
        ).run(task, "consume", allowed={"echo"})

    handlers.register("agent", consume)
    await Worker(store, handlers, lease_seconds=300).tick()
    state = runtime_invariants(store, run)["next"].agent
    assert state.snapshot.status == "waiting_approval" and not dispatched
    assert state.approvals[0].decision == "pending"
