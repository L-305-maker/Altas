from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import update
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_model_governance import due, submit
from test_provider_admission import policy as admission_policy
from test_provider_admission import reservation, worker

from agentflow.application.agents.durable_runner import DurableAgentConfig
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelRateLimitedError,
    ModelResponse,
)
from agentflow.application.ports.model_usage import (
    ModelUsage,
    UsageMapping,
    UsageMappingRegistry,
)
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import RunRow, StepRow
from agentflow.infrastructure.persistence.store import Store


def usage_policy(**values):
    return ObservedUsagePolicy(policy_version="1", **values)


def reported(message, raw):
    return ModelResponse(
        assistant_message=message,
        finish_reason="tool_calls" if message.get("tool_calls") else "stop",
        usage=raw,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["input_tokens", "output_tokens", "total_tokens"])
async def test_exact_limit_preserves_completed_action_and_never_requests_second_permit(
    store, runtime_invariants, field
):
    config = admission_policy()
    agents = SQLAgentStore(store, admission_policy=config)
    run = submit(
        store, observed_usage=usage_policy(**{f"max_observed_{field}": 5}).model_dump()
    )
    invoked = []

    async def invoke(args):
        invoked.append(args)
        return {"value": 1}

    provider = MockProvider([reported(proposal(), {field: 5}), final()])
    assert await worker(
        store, provider, config, agents=agents, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "exhausted" and provider.calls == 1
    assert state.actions[0].action.status == "succeeded" and len(invoked) == 1
    assert len(state.model_attempts) == len(state.model_permits) == 1
    assert state.observed_usage.blocked
    assert (
        state.snapshot.budget.model_attempts == state.snapshot.budget.model_rounds == 1
    )
    assert (
        runtime_invariants(store, run)["work"].step["error"]
        == "AgentObservedUsageLimit"
    )


@pytest.mark.asyncio
async def test_final_response_crossing_soft_limit_is_saved_with_artifact(
    store, runtime_invariants
):
    config = admission_policy()
    run = submit(
        store, observed_usage=usage_policy(max_observed_total_tokens=1).model_dump()
    )
    provider = MockProvider([reported(final("complete"), {"total_tokens": 20})])
    assert await worker(store, provider, config).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert (
        state.snapshot.status == "succeeded"
        and state.usage_summary().total_tokens.value == 20
    )
    assert store.artifacts(run)[0]["content"]["text"] == "complete"
    assert len(state.model_permits) == 1 and state.model_permits[0].status == "released"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fail_closed", "continue_with_warning"])
async def test_failed_429_without_usage_keeps_attempt_and_cooldown_then_governs_retry(
    store, runtime_invariants, advance_database_clock, mode
):
    config = admission_policy()
    run = submit(
        store,
        max_attempts=3,
        observed_usage=usage_policy(
            max_observed_total_tokens=10,
            missing_usage_policy=mode,
        ).model_dump(),
    )
    provider = MockProvider([ModelRateLimitedError(retry_after_seconds=5), final()])
    assert await worker(store, provider, config).tick()
    before = runtime_invariants(store, run)["work"]
    assert before.step["status"] == "ready" and before.step["error"] == "ModelRetryable"
    assert before.agent.model_attempts[0].outcome == "failed"
    assert before.agent.model_attempts[0].raw_usage is None
    advance_database_clock(6)
    store.engine.dispose()
    assert await worker(store, provider, config).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts[0] == before.agent.model_attempts[0]
    if mode == "fail_closed":
        assert state.snapshot.status == "exhausted" and provider.calls == 1
        assert len(state.model_permits) == 1
    else:
        assert state.snapshot.status == "succeeded" and provider.calls == 2
        output = store.artifacts(run)[0]["content"]
        assert output["observed_usage"]["partial"]
        assert output["observed_usage"]["summary"]["total_tokens"]["value"] is None


def test_policy_is_bound_to_run_and_ordinary_inputs_cannot_raise_it(store):
    policy = usage_policy(max_observed_input_tokens=5)
    agents = SQLAgentStore(store, observed_usage_policy=policy)
    run = submit(
        store, observed_usage=usage_policy(max_observed_input_tokens=10).model_dump()
    )
    task = store.claim(300)
    task = replace(task, inputs={"observed_usage": {"max_observed_input_tokens": 999}})
    initial = AgentSnapshot(
        id=uuid4().hex, budget=DurableAgentConfig().initial_budget()
    )
    state = agents.open(task, initial)
    assert state.observed_usage.policy.max_observed_input_tokens == 5
    with store.transaction() as db:
        assert db.get(RunRow, run).usage_governance is not None
    with pytest.raises(StateConflict):
        SQLAgentStore(
            store, observed_usage_policy=usage_policy(max_observed_input_tokens=20)
        ).open(task, initial)
    with pytest.raises(LostLease):
        agents.commit_transition(replace(task, token="wrong"), reservation(state))
    assert agents.load(task) == state


@pytest.mark.asyncio
async def test_mapping_conflict_never_persists_forged_usage(store, runtime_invariants):
    config = admission_policy()
    run = submit(store)
    response = ModelResponse(
        assistant_message=final(),
        finish_reason="stop",
        usage={"input_tokens": 1},
        normalized_usage=ModelUsage(input_tokens=90, source="provider_reported"),
    )
    provider = MockProvider([response])
    assert await worker(store, provider, config).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "failed" and provider.calls == 1
    assert state.usage_summary().input_tokens.value is None
    assert state.model_attempts[0].outcome == "failed"


@pytest.mark.asyncio
async def test_unknown_keeps_unmeasured_fact_and_held_permit(store, runtime_invariants):
    config = admission_policy()
    run = submit(
        store,
        observed_usage=usage_policy(
            max_observed_total_tokens=10,
            missing_usage_policy="continue_with_warning",
        ).model_dump(),
    )
    provider = MockProvider([ModelOutcomeUnknown(), final()])
    assert await worker(store, provider, config).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "failed" and provider.calls == 1
    assert state.observed_usage.partial
    assert state.observed_usage.summary.unknown_outcomes == 1
    assert state.observed_usage.summary.total_tokens.value is None
    assert state.model_permits[0].status == "unknown"


@pytest.mark.asyncio
async def test_result_ack_loss_restores_same_governance_without_model_replay(
    store, runtime_invariants, monkeypatch
):
    config = admission_policy()
    agents = SQLAgentStore(store, admission_policy=config)
    run = submit(
        store,
        max_attempts=3,
        observed_usage=usage_policy(max_observed_total_tokens=5).model_dump(),
    )
    invoked = []

    async def invoke(args):
        invoked.append(args)
        return {"value": 1}

    provider = MockProvider([reported(proposal(), {"total_tokens": 5}), final()])
    original = agents.commit_transition

    def lost_ack(task, transition):
        state = original(task, transition)
        if (
            state.snapshot.status == "actions_pending"
            and state.snapshot.actions[0].status == "proposed"
        ):
            raise AgentStoreUnavailable("injected lost acknowledgement")
        return state

    monkeypatch.setattr(agents, "commit_transition", lost_ack)
    assert await worker(
        store, provider, config, agents=agents, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    before = runtime_invariants(store, run)["work"].agent
    assert before.observed_usage.blocked and len(before.model_attempts) == 1
    with store.transaction() as db:
        db.execute(update(StepRow).where(StepRow.run_id == run).values(lease_until=0))
    # Lease recovery uses the normal Step retry delay, without resetting budgets.
    assert not await worker(store, provider, config).tick()
    due(store, run)
    assert await worker(
        store, provider, config, runtime=tools(invoke), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "exhausted" and provider.calls == 1
    assert state.model_attempts == before.model_attempts and len(invoked) == 1


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_postgres_current_revision_governs_competing_reservations(store):
    config = admission_policy()
    run = submit(
        store, observed_usage=usage_policy(max_observed_total_tokens=5).model_dump()
    )
    owner = SQLAgentStore(store, admission_policy=config)
    task = store.claim(300)
    assert task.run_id == run
    state = owner.open(
        task,
        AgentSnapshot(id=uuid4().hex, budget=DurableAgentConfig().initial_budget()),
    )
    connections = [
        Store(store.engine.url.render_as_string(hide_password=False)) for _ in range(2)
    ]
    barrier = Barrier(2, timeout=5)

    def reserve(index):
        barrier.wait()
        try:
            return SQLAgentStore(
                connections[index], admission_policy=config
            ).reserve_model_attempt(task, reservation(state))
        except StateConflict:
            return None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(reserve, i) for i in range(2)]
            assert sum(f.result(timeout=10) is not None for f in futures) == 1
        saved = owner.load(task)
        assert len(saved.model_attempts) == len(saved.model_permits) == 1
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.asyncio
async def test_zero_limit_precedes_provider_admission_and_attempt_reservation(
    store, runtime_invariants, monkeypatch
):
    config = admission_policy()
    agents = SQLAgentStore(store, admission_policy=config)
    run = submit(
        store, observed_usage=usage_policy(max_observed_total_tokens=0).model_dump()
    )

    def forbidden(*args):
        pytest.fail("usage stop must precede permit acquisition")

    monkeypatch.setattr(agents.admission, "acquire", forbidden)
    provider = MockProvider()
    assert await worker(store, provider, config, agents=agents).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "exhausted" and provider.calls == 0
    assert state.model_attempts == state.model_permits == ()
    assert (
        state.snapshot.budget.model_rounds == state.snapshot.budget.model_attempts == 0
    )


@pytest.mark.asyncio
async def test_below_limit_continues_and_final_crossing_does_not_erase_history(
    store, runtime_invariants
):
    config = admission_policy()
    run = submit(
        store, observed_usage=usage_policy(max_observed_total_tokens=5).model_dump()
    )

    async def tool(args):
        return {"value": 1}

    provider = MockProvider(
        [
            reported(proposal(), {"total_tokens": 2}),
            reported(final(), {"total_tokens": 4}),
        ]
    )
    assert await worker(
        store, provider, config, runtime=tools(tool), allowed={"echo"}
    ).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded" and provider.calls == 2
    assert state.observed_usage.summary.total_tokens.value == 6
    assert len(state.model_permits) == 2 and len(state.messages) == 5
    assert state.actions[0].action.status == "succeeded"
    assert len(store.artifacts(run)) == 1


def test_run_policy_binding_covers_later_agent_steps(store):
    config = usage_policy(max_observed_input_tokens=5)
    workflow = WorkflowSpec(
        name="two-usage-agents",
        steps=tuple(StepSpec(id=f"work-{i}", handler="agent") for i in range(2)),
    )
    store.submit(store.register(workflow), {})
    agents = SQLAgentStore(store, observed_usage_policy=config)
    first = store.claim(300)
    agents.open(
        first,
        AgentSnapshot(id=uuid4().hex, budget=DurableAgentConfig().initial_budget()),
    )
    second = store.claim(300)
    assert second.step_id != first.step_id
    initial = AgentSnapshot(
        id=uuid4().hex, budget=DurableAgentConfig().initial_budget()
    )
    with pytest.raises(StateConflict):
        SQLAgentStore(store).open(second, initial)
    assert agents.open(second, initial).observed_usage.policy == config


def test_custom_mapping_history_can_be_cancelled_without_live_provider_registration(
    store,
):
    from test_agent_store import reserve, response, start

    from agentflow.application.ports.agent_store import AgentTransition

    definition = UsageMapping(provider="fixture-custom", version="1", input_field="in")
    agents = SQLAgentStore(store, usage_mappings=UsageMappingRegistry((definition,)))
    task, state = start(agents)
    state = reserve(agents, task, state)
    transition = response(state)
    transition = AgentTransition.model_validate(
        {
            **transition.model_dump(),
            "model_result": {
                **transition.model_result.model_dump(),
                "usage": {"in": 3},
                "normalized_usage": definition.normalize({"in": 3}).model_dump(),
            },
        }
    )
    saved = agents.commit_transition(task, transition)
    assert saved.model_attempts[0].usage.mapping == definition
    store.cancel(task.run_id)
    with SQLAgentStore(store)._operator_transaction(saved.snapshot.id) as (
        _,
        _,
        _,
        _,
        restored,
    ):
        assert restored.snapshot.status == "cancelled"
        assert restored.model_attempts == saved.model_attempts


def test_custom_mapping_rules_cannot_drift_during_run(store):
    from test_agent_store import start

    first = UsageMapping(provider="fixture-custom", version="1", input_field="in")
    owner = SQLAgentStore(store, usage_mappings=UsageMappingRegistry((first,)))
    task, ready = start(owner)
    drift = UsageMapping(provider="fixture-custom", version="1", input_field="other")
    changed = SQLAgentStore(store, usage_mappings=UsageMappingRegistry((drift,)))
    with pytest.raises(StateConflict):
        changed.commit_transition(task, reservation(ready))
    assert owner.load(task) == ready


def test_conflicting_direct_result_rolls_back_snapshot_and_ledger(store):
    from test_agent_store import reserve, response, start

    from agentflow.application.ports.agent_store import AgentDataError, AgentTransition

    agents = SQLAgentStore(store)
    task, ready = start(agents)
    inflight = reserve(agents, task, ready)
    result = response(inflight)
    bad = AgentTransition.model_validate(
        {
            **result.model_dump(),
            "model_result": {
                **result.model_result.model_dump(),
                "usage": {"input_tokens": 1},
                "normalized_usage": ModelUsage(
                    input_tokens=9, source="provider_reported"
                ).model_dump(),
            },
        }
    )
    with pytest.raises(AgentDataError):
        agents.commit_transition(task, bad)
    assert agents.load(task) == inflight


@pytest.mark.asyncio
async def test_approval_and_reconciliation_keep_usage_governance(
    store, runtime_invariants
):
    from test_action_review_runner import setup

    from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision

    run, _, runtime, provider, invoked, _, _ = setup(store, unknown=True)
    config = admission_policy()
    agents = SQLAgentStore(
        store,
        admission_policy=config,
        observed_usage_policy=usage_policy(
            max_observed_total_tokens=100,
            missing_usage_policy="continue_with_warning",
        ),
    )
    active = worker(
        store, provider, config, agents=agents, runtime=runtime, allowed={"write"}
    )
    assert await active.tick()
    pending = agents.list_pending_action_approvals()[0]
    waiting = runtime_invariants(store, run)["work"].agent
    agents.decide_action(
        pending.approval_id,
        ApprovalDecision(
            expected_revision=waiting.snapshot.revision,
            decision="approved",
            actor="fixture",
        ),
    )
    assert await active.tick()
    unknown = runtime_invariants(store, run)["work"].agent
    assert unknown.snapshot.status == "waiting_reconciliation"
    assert provider.calls == 1 and unknown.observed_usage.partial
    agents.reconcile_action(
        pending.session_id,
        pending.action_id,
        ReconciliationDecision(
            request_id=uuid4().hex,
            expected_revision=unknown.snapshot.revision,
            confirmed_status="succeeded",
            result={"value": 1},
            actor="fixture",
            evidence_reference="fixture-receipt",
        ),
    )
    assert await active.tick()
    final_state = runtime_invariants(store, run)["work"].agent
    assert final_state.snapshot.status == "succeeded" and len(invoked) == 1
    assert final_state.observed_usage.policy == waiting.observed_usage.policy
