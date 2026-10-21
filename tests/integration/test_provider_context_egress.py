import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_model_governance import due, submit, worker
from test_provider_admission import policy as admission_policy
from test_provider_admission import reservation
from test_provider_admission import worker as admitted_worker

from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.agents.durable_runner import (
    DurableAgentConfig,
    DurableAgentRunner,
)
from agentflow.application.agents.egress import prepare_egress
from agentflow.application.agents.projection import project_context
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.context_egress import ProviderContextEgressPolicy
from agentflow.application.ports.context_projection import (
    ContextProjectionError,
    ContextProjectionPolicy,
    ContextProjectionReservation,
)
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import RunRow
from agentflow.infrastructure.persistence.store import Store

PRIVATE = "SYNTHETIC_EGRESS_PRIVATE_VALUE"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "recent_complete_rounds"])
async def test_explicit_metadata_policy_filters_actual_provider_without_changing_audit(
    store, runtime_invariants, mode
):
    async def invoke(_):
        return {"private": PRIVATE, "public": "公开值"}

    run = submit(
        store,
        context_projection={"mode": mode},
        provider_context_egress={"disclosure_mode": "metadata_only"},
    )
    provider = MockProvider([proposal(), final()])
    assert await worker(store, provider, runtime=tools(invoke)).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded"
    assert provider.calls == 2
    if PRIVATE in json.dumps(provider.contexts):
        pytest.fail("ToolObservationEgressNotAuthorized", pytrace=False)
    assert state.actions[0].result == {"private": PRIVATE, "public": "公开值"}
    assert any(
        m.payload.get("result", {}).get("private") == PRIVATE
        for m in state.messages
        if m.role == "tool"
    )
    assert state.model_attempts[-1].context_projection.egress is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["full", "recent_complete_rounds"])
async def test_historical_version_mismatch_never_inherits_same_name_public_rule(
    store, runtime_invariants, mode
):
    invoked = []

    async def invoke(_):
        invoked.append(1)
        runtime._tools["echo"] = replace(runtime._tools["echo"], version="v2")
        return {
            "value": "PUBLIC_FIRST" if len(invoked) == 1 else PRIVATE,
            "private": PRIVATE,
        }

    runtime = tools(invoke, version="v1")
    egress = {
        "disclosure_mode": "bound_disclosure",
        "rules": [
            {
                "tool_name": "echo",
                "binding": runtime.binding("echo").model_dump(mode="json"),
                "result_path": ["value"],
                "mode": "approved_excerpt",
                "classification": "public",
            }
        ],
    }
    run = submit(
        store, context_projection={"mode": mode}, provider_context_egress=egress
    )
    provider = MockProvider([proposal(), proposal(), final()])
    assert await worker(store, provider, runtime=runtime).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded" and len(invoked) == 2
    raw = json.dumps(provider.contexts[-1])
    assert "PUBLIC_FIRST" in raw
    if PRIVATE in raw:
        pytest.fail("ToolObservationBindingMismatch", pytrace=False)
    assert [a.tool_binding.tool_version for a in state.actions] == ["v1", "v2"]
    assert state.actions[-1].result["value"] == PRIVATE
    assert len([m for m in provider.contexts[-1] if m["role"] == "tool"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("composition", ["metadata", "public", "conflict"])
async def test_checkpoint_composition_has_one_renderer_or_stops_before_reservation(
    store, runtime_invariants, composition
):
    from test_durable_evidence_checkpoint import CONFIG

    async def invoke(_):
        return {"value": "PUBLIC", "private": PRIVATE}

    disclosure = (
        CONFIG["evidence_checkpoint"]["disclosure"]
        if composition != "metadata"
        else {"policy_version": 2}
    )
    egress = (
        {"disclosure_mode": "metadata_only"}
        if composition != "public"
        else {
            "disclosure_mode": "bound_disclosure",
            "max_disclosure_bytes": 4096,
            "rules": disclosure["rules"],
        }
    )
    run = submit(
        store,
        **{
            **CONFIG,
            "evidence_checkpoint": {
                **CONFIG["evidence_checkpoint"],
                "disclosure": disclosure,
            },
        },
        provider_context_egress=egress,
    )
    provider = MockProvider([proposal(), final()])
    assert await worker(store, provider, runtime=tools(invoke)).tick()
    state = runtime_invariants(store, run)["work"]
    if composition == "conflict":
        assert (
            state.step["error"] == "AgentContextEgressPolicyConflict"
            and provider.calls == 0
        )
        assert (
            state.agent.model_attempts == ()
            and state.agent.snapshot.budget.model_attempts == 0
        )
    else:
        assert state.agent.snapshot.status == "succeeded" and provider.calls == 2
        assert state.agent.actions[0].result["private"] == PRIVATE
        assert PRIVATE not in json.dumps(provider.contexts)
        assert ("PUBLIC" in json.dumps(provider.contexts)) == (composition == "public")


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_retry_egress_identity_survives_restart_and_policy_drift_fails_closed(
    store, runtime_invariants, drift
):
    run = submit(store, provider_context_egress={"disclosure_mode": "metadata_only"})
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider).tick()
    before = runtime_invariants(store, run)["work"].agent
    due(store, run)
    if drift:
        with store.transaction() as db:
            row = db.get(RunRow, run)
            spec = json.loads(json.dumps(row.spec))
            spec["steps"][0]["parameters"]["agent"]["provider_context_egress"][
                "max_disclosure_bytes"
            ] = 128
            row.spec = spec  # Simulate an invalid policy rewrite during deployment.
    store.engine.dispose()
    assert await worker(store, provider).tick()
    after = runtime_invariants(store, run)["work"]
    if drift:
        assert after.step["error"] == "AgentContextProjectionChanged"
        assert (
            provider.calls == 1 and after.agent.model_attempts == before.model_attempts
        )
        assert after.agent.snapshot.budget == before.snapshot.budget
    else:
        first, second = after.agent.model_attempts
        assert first.context_projection == second.context_projection
        assert first.request_contract == second.request_contract
        assert first.model_call_id == second.model_call_id
        assert after.agent.snapshot.budget.model_attempts == 2
        assert after.agent.snapshot.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["usage", "admission", "context"])
async def test_egress_does_not_bypass_preflight_gates(store, runtime_invariants, gate):
    from test_observed_usage_governance import reported, usage_policy
    from test_provider_admission import opened

    config = admission_policy()
    agents = SQLAgentStore(store, admission_policy=config)
    options = {"provider_context_egress": {"disclosure_mode": "metadata_only"}}
    if gate == "usage":
        options["observed_usage"] = usage_policy(
            max_observed_total_tokens=1
        ).model_dump()
    elif gate == "context":
        options["max_context_bytes"] = 1
    else:
        task, state = opened(agents)
        agents.reserve_model_attempt(task, reservation(state))
    run = submit(store, **options)
    provider = MockProvider([reported(proposal(), {"total_tokens": 1}), final()])

    async def invoke(_):
        return {"private": PRIVATE}

    assert await admitted_worker(
        store, provider, config, agents=agents, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"]
    assert provider.calls == (1 if gate == "usage" else 0)
    assert (
        len(state.agent.model_attempts)
        == len(state.agent.model_permits)
        == provider.calls
    )
    assert (
        state.step["error"]
        == {
            "usage": "AgentObservedUsageLimit",
            "admission": "ProviderAdmissionDeferred",
            "context": "AgentContextLimit",
        }[gate]
    )


def opened_egress(agents):
    run = submit(
        agents.store, provider_context_egress={"disclosure_mode": "metadata_only"}
    )
    task = agents.store.claim(300)
    assert task.run_id == run
    state = agents.open(
        task,
        AgentSnapshot(
            id=task.idempotency_key, budget=DurableAgentConfig().initial_budget()
        ),
    )
    return task, state


def egress_reservation(state):
    full = AgentContextBuilder().build(state)
    egress = prepare_egress(full, state, ProviderContextEgressPolicy())
    projected = project_context(
        full, [], ContextProjectionPolicy(), 1_000_000, egress=egress
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


def test_store_recomputes_egress_before_permit_and_rolls_back_atomic_reservation(
    store, monkeypatch
):
    agents = SQLAgentStore(store, admission_policy=admission_policy())
    task, state = opened_egress(agents)
    proposed = egress_reservation(state)
    data = proposed.model_dump()
    data["context_projection"]["manifest"]["egress"]["policy_digest"] = "0" * 64
    with monkeypatch.context() as patch:
        patch.setattr(
            agents.admission,
            "acquire",
            lambda *_: pytest.fail("forged egress cannot acquire permit"),
        )
        with pytest.raises(ContextProjectionError):
            agents.reserve_model_attempt(task, AgentTransition.model_validate(data))
        with pytest.raises(ContextProjectionError):
            agents.reserve_model_attempt(task, reservation(state))
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


@pytest.mark.asyncio
async def test_ack_loss_reads_committed_egress_and_never_resends_unknown(
    store, runtime_invariants, monkeypatch
):
    agents = SQLAgentStore(store)
    run = submit(store, provider_context_egress={"disclosure_mode": "metadata_only"})
    task = store.claim(300)
    original = agents.commit_transition

    def lost_ack(task, transition):
        original(task, transition)
        raise AgentStoreUnavailable("synthetic commit acknowledgement lost")

    monkeypatch.setattr(agents, "commit_transition", lost_ack)
    provider = MockProvider()
    runner = DurableAgentRunner(agents, provider, ToolRuntime())
    with pytest.raises(AgentStoreUnavailable):
        await runner.run(task, "goal", allowed=set())
    before = runtime_invariants(store, run)["work"].agent
    assert (
        len(before.model_attempts) == 1
        and before.model_attempts[0].context_projection.egress is not None
    )
    store.engine.dispose()
    result = await asyncio.wait_for(runner.run(task, "goal", allowed=set()), timeout=10)
    after = runtime_invariants(store, run)["work"].agent
    assert result.status == "failed" and provider.calls == 0
    assert after.model_attempts[0].outcome == "unknown"
    assert (
        after.model_attempts[0].context_projection
        == before.model_attempts[0].context_projection
    )
    assert after.snapshot.budget == before.snapshot.budget


@pytest.mark.asyncio
async def test_oversized_actual_tool_result_keeps_unknown_and_never_skips_to_model(
    store, runtime_invariants
):
    calls = []

    async def invoke(_):
        calls.append(1)
        return {"value": "x" * 100_001}

    run = submit(store, provider_context_egress={"disclosure_mode": "metadata_only"})
    provider = MockProvider([proposal(), final()])
    executor = worker(store, provider, runtime=tools(invoke))
    assert await executor.tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "waiting_reconciliation"
    assert (
        state.actions[0].action.status == "unknown" and state.actions[0].result is None
    )
    assert len(calls) == provider.calls == 1
    assert not await executor.tick()
    assert runtime_invariants(store, run)["work"].agent == state


def test_egress_manifest_capacity_keeps_cancel_cleanup_space(store, monkeypatch):
    from test_action_reviews import fill_to_admission_limit

    import agentflow.infrastructure.persistence.agent_store as module

    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 120_000)
    agents = SQLAgentStore(store)
    task, ready = opened_egress(agents)
    pending = agents.commit_transition(task, egress_reservation(ready))
    filled = fill_to_admission_limit(agents, task, pending)
    store.cancel(task.run_id)
    with agents._operator_transaction(filled.snapshot.id) as (_, _, step, _, restored):
        assert restored.snapshot.status == step.status == "cancelled"
        assert restored.model_attempts[0].outcome == "unknown"
        assert (
            restored.model_attempts[0].context_projection
            == filled.model_attempts[0].context_projection
        )
        assert restored.messages == filled.messages


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_independent_postgres_egress_reservation_has_one_cas_winner(
    store, runtime_invariants
):
    agents = SQLAgentStore(store, admission_policy=admission_policy())
    task, state = opened_egress(agents)
    proposed = egress_reservation(state)
    barrier = Barrier(2, timeout=10)

    def reserve():
        other = Store(store.engine.url.render_as_string(hide_password=False))
        try:
            barrier.wait()
            return SQLAgentStore(
                other, admission_policy=admission_policy()
            ).reserve_model_attempt(task, proposed)
        except StateConflict:
            return None
        finally:
            other.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        outcomes = [f.result(timeout=20) for f in futures]
    assert sum(o is not None for o in outcomes) == 1
    saved = runtime_invariants(store, task.run_id)["work"].agent
    assert len(saved.model_attempts) == len(saved.model_permits) == 1
    assert (
        saved.model_attempts[0].context_projection
        == proposed.context_projection.manifest
    )
