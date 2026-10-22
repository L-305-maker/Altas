from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import store as store
from test_durable_summary import (
    SummaryProvider,
    setup_summary,
    summary_due,
    summary_worker,
)

from agentflow.application.agents.summary import SummarySourceError
from agentflow.application.agents.summary_runner import DurableSummaryRunner
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import AgentSessionRow, RunRow
from agentflow.infrastructure.persistence.store import Store


@pytest.mark.asyncio
async def test_policy_drift_cannot_prevent_summary_cancellation_cleanup(store):
    import json

    run, agents, _ = await setup_summary(store)
    task, state, proposed = open_summary(agents)
    pending = agents.commit_transition(task, proposed)
    with store.transaction() as db:
        row = db.get(RunRow, run)
        data = json.loads(json.dumps(row.spec))
        data["steps"][1]["parameters"]["summary"]["enabled"] = False
        row.spec = data  # Drift must block dispatch, not cancellation.
    store.cancel(run)
    with agents._operator_transaction(pending.snapshot.id) as (_, _, step, _, restored):
        assert step.status == "cancelled" and restored.snapshot.status == "cancelled"
        assert restored.model_attempts[0].outcome == "unknown"


@pytest.mark.asyncio
async def test_reconciled_summary_read_cannot_replace_frozen_authorized_input(
    store, runtime_invariants, monkeypatch
):
    from test_durable_agent_runner import final

    from agentflow.application.ports.agent_store import AgentDataError
    from agentflow.domain.agent.review import ReconciliationDecision
    from agentflow.infrastructure.models.mock import MockProvider

    run, agents, _ = await setup_summary(store)
    original = agents.commit_transition

    def lose_result(task, transition):
        if any(a.status == "succeeded" for a in transition.snapshot.actions):
            raise AgentDataError("synthetic result limit")
        return original(task, transition)

    with monkeypatch.context() as patch:
        patch.setattr(agents, "commit_transition", lose_result)
        assert await summary_worker(store, agents, SummaryProvider()).tick()
    waiting = runtime_invariants(store, run)["summary"].agent
    assert waiting.snapshot.status == "waiting_reconciliation"
    agents.reconcile_action(
        waiting.snapshot.id,
        waiting.actions[0].action.id,
        ReconciliationDecision(
            request_id="summary-false-read",
            expected_revision=waiting.snapshot.revision,
            confirmed_status="succeeded",
            result={"private": "SYNTHETIC_UNAUTHORIZED_RECONCILIATION"},
            actor="test",
            evidence_reference="synthetic",
        ),
    )
    provider = MockProvider([final("{}")])
    assert await summary_worker(store, agents, provider).tick()
    assert provider.calls == 0, "SummaryEvidenceReplacedBeforeProvider"
    assert len(store.artifacts(run)) == 1


@pytest.mark.asyncio
async def test_summary_cannot_widen_explicit_source_metadata_policy(store):
    run, agents, _ = await setup_summary(
        store,
        source_agent={"provider_context_egress": {"disclosure_mode": "metadata_only"}},
    )
    task = store.claim(300)
    with pytest.raises(SummarySourceError, match="SummarySourceDisclosureConflict"):
        agents.summary_source(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reservation", "completion", "rollback"])
async def test_summary_commit_faults_do_not_duplicate_attempt_or_artifact(
    store, runtime_invariants, monkeypatch, boundary
):
    run, agents, _ = await setup_summary(store)
    source = runtime_invariants(store, run)["source"].agent
    task = store.claim(300)
    provider = SummaryProvider()
    if boundary == "rollback":

        def fail(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO agent_model_attempts"):
                raise OperationalError("synthetic", None, None)

        event.listen(store.engine, "before_cursor_execute", fail)
    else:
        method = (
            "commit_transition" if boundary == "reservation" else "complete_agent_step"
        )
        original = getattr(agents, method)

        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise AgentStoreUnavailable("synthetic acknowledgement lost")

        monkeypatch.setattr(agents, method, lost_ack)
    try:
        with pytest.raises(AgentStoreUnavailable):
            await DurableSummaryRunner(agents, provider).run(task)
    finally:
        if boundary == "rollback":
            event.remove(store.engine, "before_cursor_execute", fail)
    state = runtime_invariants(store, run)["summary"].agent
    assert runtime_invariants(store, run)["source"].agent == source
    if boundary == "rollback":
        assert state.model_attempts == () and provider.calls == 0
        assert state.snapshot.budget.model_attempts == 0
    elif boundary == "completion":
        assert state.snapshot.status == "succeeded" and len(store.artifacts(run)) == 2
        with pytest.raises(LostLease):
            await DurableSummaryRunner(agents, provider).run(task)
        assert (
            provider.calls == 2
            and not await summary_worker(store, agents, provider).tick()
        )
    else:
        assert len(state.model_attempts) == 1 and provider.calls == 0
        store.engine.dispose()
        result = await DurableSummaryRunner(agents, provider).run(task)
        after = runtime_invariants(store, run)["summary"].agent
        assert (
            result.status == "failed" and after.model_attempts[0].outcome == "unknown"
        )
        assert (
            after.model_attempts[0].context_projection
            == state.model_attempts[0].context_projection
        )
        assert provider.calls == 0 and len(store.artifacts(run)) == 1


@pytest.mark.asyncio
async def test_summary_artifact_insert_failure_rolls_back_model_confirmation(
    store, runtime_invariants
):
    run, agents, _ = await setup_summary(store)
    task = store.claim(300)
    provider = SummaryProvider()

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO artifacts"):
            raise OperationalError("synthetic", None, None)

    event.listen(store.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AgentStoreUnavailable):
            await DurableSummaryRunner(agents, provider).run(task)
    finally:
        event.remove(store.engine, "before_cursor_execute", fail)
    state = runtime_invariants(store, run)["summary"].agent
    assert (
        state.snapshot.status == "model_inflight"
        and state.model_attempts[-1].outcome == "inflight"
    )
    assert len(store.artifacts(run)) == 1 and provider.calls == 2
    result = await DurableSummaryRunner(agents, provider).run(task)
    assert result.status == "failed" and provider.calls == 2
    assert (
        runtime_invariants(store, run)["summary"].agent.model_attempts[-1].outcome
        == "unknown"
    )


@pytest.mark.asyncio
async def test_summary_source_revision_drift_stops_before_next_provider_request(
    store, runtime_invariants
):
    run, agents, _ = await setup_summary(store)
    provider = SummaryProvider(failures=[ModelRetryableError()])
    assert await summary_worker(store, agents, provider).tick()
    before = runtime_invariants(store, run)
    summary_due(store, run)
    with store.transaction() as db:
        row = db.get(AgentSessionRow, before["source"].agent.snapshot.id)
        row.snapshot = {**row.snapshot, "revision": row.revision + 1}
        row.revision += 1  # External corruption: normal terminal writes forbid this.
    task = store.claim(300)
    with pytest.raises(SummarySourceError, match="SummarySourceChanged"):
        await DurableSummaryRunner(agents, provider).run(task)
    assert provider.calls == 1
    # The source was deliberately corrupted; inspect only unaffected summary
    # facts instead of concealing the shared terminal-immutability failure.
    with agents._operator_transaction(before["summary"].agent.snapshot.id) as (
        _,
        _,
        _,
        _,
        restored,
    ):
        assert restored.model_attempts == before["summary"].agent.model_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["admission", "usage", "capacity"])
async def test_summary_respects_admission_usage_and_input_capacity(
    store, runtime_invariants, gate
):
    from test_provider_admission import policy

    from agentflow.application.ports.observed_usage import ObservedUsagePolicy

    changes = {}
    if gate == "usage":
        changes["observed_usage"] = ObservedUsagePolicy(
            policy_version="1", max_observed_total_tokens=1
        ).model_dump(mode="json")
    if gate == "capacity":
        changes["max_source_bytes"] = 256
    run, agents, _ = await setup_summary(store, **changes)
    provider = SummaryProvider(usage={"total_tokens": 1})
    if gate == "admission":
        agents = SQLAgentStore(
            store, admission_policy=policy(max_requests_per_window=1)
        )
        task, state, proposed = open_summary(agents)
        held = agents.reserve_model_attempt(task, proposed)
        assert held.status == "reserved"
        # A separate summary workflow must defer under the same provider key.
        run, _, _ = await setup_summary(store)
    if gate == "capacity":
        task = store.claim(300)
        with pytest.raises(SummarySourceError, match="SummaryInputLimit"):
            await DurableSummaryRunner(agents, provider).run(task)
        assert provider.calls == 0
    else:
        assert await summary_worker(store, agents, provider).tick()
        state = runtime_invariants(store, run)["summary"]
        assert provider.calls == (1 if gate == "usage" else 0)
        assert len(state.agent.model_attempts) == provider.calls
        assert state.step["error"] == (
            "AgentObservedUsageLimit"
            if gate == "usage"
            else "ProviderAdmissionDeferred"
        )


def open_summary(agents):
    import hashlib

    from test_provider_admission import reservation

    from agentflow.application.agents.context import AgentContextBuilder
    from agentflow.application.agents.durable_runner import DurableAgentConfig
    from agentflow.application.agents.egress import prepare_egress
    from agentflow.application.agents.projection import project_context
    from agentflow.application.agents.summary import (
        _never,
        summary_agent_parameters,
        summary_messages,
        summary_tool,
    )
    from agentflow.application.ports.agent_store import AgentTransition
    from agentflow.application.ports.context_projection import (
        ContextProjectionReservation,
    )
    from agentflow.application.tools.runtime import ToolRuntime
    from agentflow.domain.agent.run import AgentSnapshot
    from agentflow.infrastructure.models.mock import MockProvider

    task = agents.store.claim(300)
    plan = agents.summary_source(task)
    config = DurableAgentConfig(**summary_agent_parameters(task.spec))
    state = agents.open(
        task,
        AgentSnapshot(
            id=hashlib.sha256(task.idempotency_key.encode()).hexdigest(),
            budget=config.initial_budget(),
        ),
        messages=summary_messages(plan),
    )
    runtime = ToolRuntime()
    runtime.register(summary_tool(_never))
    schemas = runtime.schemas({"read_summary_evidence"})
    full = AgentContextBuilder().build(state)
    projected = project_context(
        full,
        schemas,
        config.context_projection,
        config.max_context_bytes,
        egress=prepare_egress(full, state, config.provider_context_egress),
        summary_input_digest=plan.manifest.summary_input_digest,
    )
    proposed = AgentTransition.model_validate(
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
    return task, state, proposed


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["postgres"], indirect=True)
async def test_independent_postgres_summary_reservation_cas_and_fencing(
    store, runtime_invariants
):
    from test_provider_admission import policy

    run, _, _ = await setup_summary(store)
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state, proposed = open_summary(agents)
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(replace(task, token="stale"), proposed)
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
        results = [f.result(timeout=20) for f in futures]
    assert sum(r is not None for r in results) == 1
    saved = runtime_invariants(store, run)["summary"].agent
    assert len(saved.model_attempts) == len(saved.model_permits) == 1
    assert (
        saved.model_attempts[0].request_contract.summary_input_digest
        == proposed.request_contract.summary_input_digest
    )
