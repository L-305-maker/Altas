import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_model_governance import due, submit, worker
from test_observed_usage_governance import reported, usage_policy
from test_provider_admission import opened, policy, reservation
from test_provider_admission import worker as admitted_worker

from agentflow.application.agents.checkpoint import build_checkpoint
from agentflow.application.agents.context import AgentContextBuilder, context_bytes
from agentflow.application.agents.durable_runner import (
    DurableAgentConfig,
    DurableAgentRunner,
)
from agentflow.application.agents.projection import project_context
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionPolicy,
    ContextProjectionReservation,
)
from agentflow.application.ports.evidence_checkpoint import EvidenceCheckpointPolicy
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store

CONFIG = {
    "context_projection": {"mode": "evidence_anchored_recent_rounds"},
    "evidence_checkpoint": {"mode": "anchored_extracts"},
}


def opened_checkpoint(agents):
    run = submit(agents.store, **CONFIG)
    task = agents.store.claim(300)
    assert task.run_id == run
    return task, agents.open(
        task,
        AgentSnapshot(
            id=task.idempotency_key, budget=DurableAgentConfig().initial_budget()
        ),
    )


def evidence_reservation(state):
    projected = project_context(
        AgentContextBuilder().build(state),
        [],
        ContextProjectionPolicy(**CONFIG["context_projection"]),
        1_000_000,
        checkpoint=build_checkpoint(
            state, EvidenceCheckpointPolicy(**CONFIG["evidence_checkpoint"])
        ),
    )
    return AgentTransition.model_validate(
        {
            **reservation(state).model_dump(),
            "context_projection": ContextProjectionReservation(
                manifest=projected.manifest
            ),
            "request_contract": MockProvider()
            .prepare_request(projected.messages, [], projected.manifest)
            .request.contract,
        }
    )


@pytest.mark.asyncio
async def test_worker_checkpoint_projection_preserves_full_audit_and_request(
    store, runtime_invariants
):
    run = submit(store, **CONFIG, max_context_bytes=8500)
    provider = MockProvider([proposal(), proposal(), final()])

    async def invoke(_):
        return {"value": "中文证据" * 400}

    assert await worker(store, provider, runtime=tools(invoke)).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded"
    assert len(state.actions) == 2 and len(state.messages) == 7
    assert all(a.result == {"value": "中文证据" * 400} for a in state.actions)
    last = state.model_attempts[-1]
    assert (
        context_bytes(
            AgentContextBuilder().build(state), tools(invoke).schemas({"echo"})
        )
        > 8500
    )
    assert last.context_projection.projected_context_bytes <= 8500
    assert last.context_projection.omitted_round_count == 1
    assert last.context_projection.checkpoint.included_evidence_count == 3
    last.request_contract.verify_context(
        provider.contexts[-1], tools(invoke).schemas({"echo"})
    )
    assert len(store.artifacts(run)) == 1


@pytest.mark.asyncio
async def test_retry_checkpoint_identity_is_restored(store, runtime_invariants):
    run = submit(store, **CONFIG)
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider).tick()
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    store.engine.dispose()
    assert await worker(store, provider).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded"
    assert len({a.model_call_id for a in state.model_attempts}) == 1
    assert state.model_attempts[0] == before.model_attempts[0]
    assert (
        state.model_attempts[0].context_projection
        == state.model_attempts[1].context_projection
    )
    assert (
        state.model_attempts[0].request_contract
        == state.model_attempts[1].request_contract
    )
    assert state.snapshot.budget.model_attempts == provider.calls == 2


@pytest.mark.asyncio
async def test_checkpoint_retry_rejects_schema_hot_reload_before_new_permit(
    store, runtime_invariants
):
    run = submit(store, **CONFIG)
    provider = MockProvider([ModelRetryableError(), final()])
    config = policy()

    async def invoke(_):
        pytest.fail("no tools requested")

    runtime = tools(invoke)
    assert await admitted_worker(
        store, provider, config, runtime=runtime, allowed={"echo"}
    ).tick()
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    runtime._tools["echo"] = replace(runtime._tools["echo"], description="new schema")
    assert await admitted_worker(
        store, provider, config, runtime=runtime, allowed={"echo"}
    ).tick()
    after = runtime_invariants(store, run)["work"]
    assert after.step["error"] == "AgentContextProjectionChanged"
    assert provider.calls == 1
    assert after.agent.model_attempts == before.model_attempts
    assert after.agent.model_permits == before.model_permits
    assert after.agent.snapshot.budget == before.snapshot.budget


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked", ["checkpoint", "bytes", "usage", "admission", "token"]
)
async def test_checkpoint_cannot_bypass_preflight_gates(
    store, runtime_invariants, blocked
):
    from agentflow.application.ports.model_request import ModelRequestOptions

    options = dict(CONFIG)
    if blocked == "checkpoint":
        options["evidence_checkpoint"] = {
            "mode": "anchored_extracts",
            "max_checkpoint_bytes": 1,
        }
    if blocked == "bytes":
        options["max_context_bytes"] = 1
    if blocked == "usage":
        options["observed_usage"] = usage_policy(
            max_observed_total_tokens=1
        ).model_dump()
    config = policy()
    agents = SQLAgentStore(store, admission_policy=config)
    if blocked == "admission":
        task, state = opened(agents)
        agents.reserve_model_attempt(task, reservation(state))
    run = submit(store, **options)
    provider = MockProvider(
        [reported(proposal(), {"total_tokens": 1}), final()],
        request_options=ModelRequestOptions(strict_token_window=blocked == "token"),
    )

    async def invoke(_):
        return {"value": 1}

    assert await admitted_worker(
        store, provider, config, agents=agents, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"]
    codes = {
        "checkpoint": "AgentEvidenceCheckpointLimit",
        "bytes": "AgentContextLimit",
        "usage": "AgentObservedUsageLimit",
        "admission": "ProviderAdmissionDeferred",
        "token": "ModelTokenCountUnavailable",
    }
    assert state.step["error"] == codes[blocked]
    expected = int(blocked == "usage")
    assert provider.calls == state.agent.snapshot.budget.model_attempts == expected
    assert len(state.agent.model_attempts) == len(state.agent.model_permits) == expected


@pytest.mark.asyncio
async def test_reconciliation_checkpoint_uses_final_action_and_keeps_unknown_audit(
    store, runtime_invariants
):
    agents = SQLAgentStore(store)
    run = submit(store, **CONFIG)
    invocations = []

    async def invoke(args):
        invocations.append(args)
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
            request_id="evidence-final",
            expected_revision=waiting.snapshot.revision,
            confirmed_status="succeeded",
            result={"value": "confirmed"},
            actor="test",
            evidence_reference="synthetic",
        ),
    )
    assert await executor.tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded" and len(invocations) == 1
    assert len([m for m in state.messages if m.role == "tool"]) == 2
    built = build_checkpoint(
        state, EvidenceCheckpointPolicy(**CONFIG["evidence_checkpoint"])
    )
    assert (
        built.items[-1].status == "succeeded" and built.items[-1].excerpt == "confirmed"
    )
    assert (
        state.model_attempts[-1]
        .context_projection.checkpoint.references[-1]
        .source_digest
        == built.items[-1].reference.source_digest
    )


@pytest.mark.parametrize("corruption", ["history", "source", "sequence", "excerpt"])
def test_store_rebuilds_sources_before_admission_and_rolls_back(
    store, monkeypatch, corruption
):
    from agentflow.application.ports.agent_store import AgentMessage
    from agentflow.domain.agent.action import digest_arguments

    agents = SQLAgentStore(store, admission_policy=policy())
    run = submit(store, **CONFIG)
    task = store.claim(300)
    assert task.run_id == run
    state = agents.open(
        task,
        AgentSnapshot(
            id=task.idempotency_key, budget=DurableAgentConfig().initial_budget()
        ),
        messages=(
            AgentMessage(
                sequence=0, role="user", payload={"content": '{"goal":"fixed goal"}'}
            ),
        ),
    )
    proposed = evidence_reservation(state)
    data = proposed.model_dump()
    manifest = data["context_projection"]["manifest"]["checkpoint"]
    if corruption == "history":
        manifest["source_history_digest"] = "0" * 64
    else:
        field = {
            "source": "source_digest",
            "sequence": "source_sequence",
            "excerpt": "excerpt_digest",
        }[corruption]
        manifest["references"][0][field] = 999 if corruption == "sequence" else "0" * 64
        # A self-consistent forged reference digest still cannot bypass Store's
        # regeneration from real source records.
        manifest["evidence_reference_digest"] = digest_arguments(
            {"references": list(manifest["references"])}
        )
    bad = AgentTransition.model_validate(data)

    def forbidden(*args):
        pytest.fail("invalid evidence cannot acquire permit")

    with monkeypatch.context() as patch:
        patch.setattr(agents.admission, "acquire", forbidden)
        with pytest.raises(ContextProjectionError):
            agents.reserve_model_attempt(task, bad)
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(replace(task, token="stale"), proposed)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO agent_model_attempts"):
            raise OperationalError("synthetic", None, None)

    event.listen(store.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.reserve_model_attempt(task, proposed)
    finally:
        event.remove(store.engine, "before_cursor_execute", fail)
    assert agents.load(task) == state


def test_checkpoint_capacity_preserves_unknown_and_cancel_cleanup(store, monkeypatch):
    from test_action_reviews import fill_to_admission_limit

    import agentflow.infrastructure.persistence.agent_store as module

    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 120_000)
    agents = SQLAgentStore(store)
    task, initial = opened_checkpoint(agents)
    state = agents.commit_transition(task, evidence_reservation(initial))
    state = fill_to_admission_limit(agents, task, state)
    manifest = state.model_attempts[0].context_projection
    store.cancel(task.run_id)
    with agents._operator_transaction(state.snapshot.id) as (_, _, step, _, restored):
        assert step.status == "cancelled" and restored.snapshot.status == "cancelled"
        assert restored.model_attempts[0].outcome == "unknown"
        assert restored.model_attempts[0].context_projection == manifest
        assert restored.messages == state.messages


@pytest.mark.asyncio
async def test_existing_0008_json_extension_preserves_all_facts_on_head_upgrade(
    store, monkeypatch, runtime_invariants
):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from test_model_request_migrations import old_facts

    run = submit(store, **CONFIG)
    assert await worker(store, MockProvider([final()])).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts[0].context_projection.checkpoint is not None
    before = old_facts(store)
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    config = Config("alembic.ini")
    try:
        command.stamp(config, "head")
        command.upgrade(config, "head")
        command.check(config)
        assert old_facts(store) == before
        store.engine.dispose()
        assert runtime_invariants(store, run)["work"].agent == state
    finally:
        with store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.mark.asyncio
async def test_commit_ack_loss_does_not_dispatch_or_recreate_checkpoint(
    store, monkeypatch, runtime_invariants
):
    agents = SQLAgentStore(store)
    run = submit(store, **CONFIG)
    task = store.claim(300)
    original = agents.commit_transition

    def lost_ack(task, transition):
        original(task, transition)
        raise AgentStoreUnavailable("synthetic")

    with monkeypatch.context() as patch:
        patch.setattr(agents, "commit_transition", lost_ack)
        with pytest.raises(AgentStoreUnavailable):
            await DurableAgentRunner(agents, MockProvider(), ToolRuntime()).run(
                task, "goal", allowed=set()
            )
    before = runtime_invariants(store, run)["work"].agent
    provider = MockProvider()
    result = await asyncio.wait_for(
        DurableAgentRunner(agents, provider, ToolRuntime()).run(
            task, "goal", allowed=set()
        ),
        10,
    )
    after = runtime_invariants(store, run)["work"].agent
    assert result.status == "failed" and provider.calls == 0
    assert (
        len(after.model_attempts) == 1 and after.model_attempts[0].outcome == "unknown"
    )
    assert (
        after.model_attempts[0].context_projection
        == before.model_attempts[0].context_projection
    )


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_postgres_independent_workers_reserve_one_checkpoint(store, runtime_invariants):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened_checkpoint(agents)
    proposed = evidence_reservation(state)
    barrier = Barrier(2, timeout=10)

    def reserve():
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
        futures = [pool.submit(reserve) for _ in range(2)]
        outcomes = [f.result(timeout=20) for f in futures]
    assert sum(result is not None for result in outcomes) == 1
    after = runtime_invariants(store, task.run_id)["work"].agent
    assert len(after.model_attempts) == len(after.model_permits) == 1
    assert (
        after.model_attempts[0].context_projection
        == proposed.context_projection.manifest
    )
