from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from test_agent_store import agents as agents
from test_agent_store import candidate, expire, reserve, response, start
from test_durable_agent_runner import final
from test_durable_agent_runner import store as store
from test_model_governance import due, submit, worker

from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelResponse,
    ModelRetryableError,
)
from agentflow.domain.agent.status import AgentStatus
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import AgentModelAttemptRow


@pytest.mark.asyncio
async def test_worker_retry_keeps_every_attempt_and_summarizes_partial_usage(
    store, runtime_invariants
):
    run = submit(store)
    provider = MockProvider(
        [
            ModelRetryableError(),
            ModelResponse(
                assistant_message=final(),
                finish_reason="stop",
                usage={"input_tokens": 7, "output_tokens": 3, "extension": {"a": 1}},
                model="offline-model",
                request_id="private-request-id",
            ),
        ]
    )
    assert await worker(store, provider).tick()
    first = runtime_invariants(store, run)["work"].agent
    assert len(first.model_attempts) == 1
    assert first.model_attempts[0].outcome == "failed"
    due(store, run)
    store.engine.dispose()
    assert await worker(store, provider).tick()
    state = runtime_invariants(store, run)["work"].agent
    attempts = state.model_attempts
    assert [a.attempt_index for a in attempts] == [1, 2]
    assert {a.model_call_id for a in attempts} == {first.snapshot.model_call_id}
    assert attempts[0] == first.model_attempts[0]
    assert [a.outcome for a in attempts] == ["failed", "succeeded"]
    assert attempts[1].usage.input_tokens == 7
    assert attempts[1].raw_usage["extension"] == {"a": 1}
    assert attempts[1].model == "offline-model"
    assert attempts[1].request_id == "private-request-id"
    summary = SQLAgentStore(store).usage_summary(state.snapshot.id)
    assert (
        summary.attempts == state.snapshot.budget.model_attempts == provider.calls == 2
    )
    assert summary.input_tokens.value == 7
    assert summary.input_tokens.reported_attempts == 1
    assert summary.input_tokens.unreported_attempts == 1
    assert summary.total_tokens.value is None
    assert summary.unavailable_usage_attempts == 1
    # No usage payload or private request identifier in operational events.
    assert "private-request-id" not in str(store.events(run))
    assert "input_tokens" not in str(store.events(run))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ModelOutcomeUnknown(), ModelRetryableError()])
async def test_unknown_and_confirmed_failure_do_not_invent_usage(
    store, error, runtime_invariants
):
    run = submit(store, max_model_attempts=1)
    assert await worker(store, MockProvider([error])).tick()
    state = runtime_invariants(store, run)["work"].agent
    attempt = state.model_attempts[0]
    assert attempt.outcome == (
        "unknown" if isinstance(error, ModelOutcomeUnknown) else "failed"
    )
    assert attempt.raw_usage is None and attempt.usage.source == "unavailable"
    assert state.usage_summary().total_tokens.value is None
    assert attempt.confirmed_at is not None


def test_reservation_and_result_roll_back_atomically_and_duplicates_do_not_charge(
    agents,
):
    task, state = start(agents)

    def fail_attempt(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO agent_model_attempts"):
            raise OperationalError("unavailable", {}, None)

    event.listen(agents.store.engine, "before_cursor_execute", fail_attempt)
    try:
        with pytest.raises(AgentStoreUnavailable):
            reserve(agents, task, state)
    finally:
        event.remove(agents.store.engine, "before_cursor_execute", fail_attempt)
    assert agents.load(task) == state
    with agents.store.transaction() as db:
        assert list(db.scalars(select(AgentModelAttemptRow))) == []
    state = reserve(agents, task, state)
    transition = response(state)
    saved = agents.commit_transition(task, transition)
    with pytest.raises(StateConflict):
        agents.commit_transition(task, transition)
    assert agents.load(task) == saved
    assert saved.model_attempts[0].outcome == "succeeded"
    assert saved.usage_summary().attempts == 1


def test_retry_reservation_does_not_erase_prior_reported_usage(agents):
    from test_durable_model_request import reservation_with_contract

    task, state = start(agents)
    state = agents.commit_transition(task, reservation_with_contract(state))
    failed = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot, status="ready_model"),
            model_result=ModelCallResult(
                model_call_id=state.snapshot.model_call_id,
                status="failed",
                usage={"input_tokens": 5, "total_tokens": 6},
            ),
        ),
    )
    retried = agents.commit_transition(task, reservation_with_contract(failed))
    assert retried.model_calls[0].usage is None
    assert retried.model_attempts[0] == failed.model_attempts[0]
    assert retried.usage_summary().total_tokens.value == 6
    assert retried.usage_summary().total_tokens.unreported_attempts == 1


def test_unknown_cannot_be_rewritten_as_confirmed_failure(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    state = agents.commit_transition(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot),
            model_result=ModelCallResult(model_call_id="model-1", status="unknown"),
        ),
    )
    with pytest.raises(StateConflict):
        agents.fail_agent_step(
            task,
            AgentTransition(
                snapshot=candidate(state.snapshot, status=AgentStatus.FAILED),
                model_result=ModelCallResult(model_call_id="model-1", status="failed"),
            ),
        )
    assert agents.load(task) == state


def test_cancel_fences_late_usage_confirmation(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    agents.store.cancel(task.run_id)
    with pytest.raises(LostLease):
        agents.commit_transition(task, response(state))
    summary = agents.usage_summary(state.snapshot.id)
    assert summary.attempts == summary.unknown_outcomes == 1
    assert summary.total_tokens.value is None


def test_reclaimed_lease_fences_old_usage_result(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    expire(agents, task)
    new_task = agents.store.claim(300)
    assert new_task.token != task.token
    restored = agents.load(new_task)
    with pytest.raises(LostLease):
        agents.commit_transition(task, response(state))
    assert agents.load(new_task) == restored
    assert restored.model_attempts[0].outcome == "unknown"
    assert restored.model_attempts[0].raw_usage is None


@pytest.mark.parametrize("agents", ["postgres"], indirect=True)
def test_postgres_competing_results_confirm_only_once(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)
    barrier = Barrier(2, timeout=10)

    def confirm():
        barrier.wait()
        try:
            return agents.commit_transition(task, response(state))
        except StateConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert agents.load(task).usage_summary().attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [None, {}, {"input_tokens": -2}, {"input_tokens": True}, {"total_tokens": 1.5}],
)
async def test_missing_or_invalid_reported_counters_remain_unavailable(
    store, raw, runtime_invariants
):
    run = submit(store)
    provider = MockProvider(
        [ModelResponse(assistant_message=final(), finish_reason="stop", usage=raw)]
    )
    assert await worker(store, provider).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "succeeded"
    attempt = state.model_attempts[0]
    assert attempt.raw_usage == raw
    assert attempt.usage.source == "unavailable"
    assert state.usage_summary().total_tokens.value is None


@pytest.mark.asyncio
async def test_invalid_final_answer_keeps_valid_provider_measurements(
    store, runtime_invariants
):
    run = submit(store)
    provider = MockProvider(
        [
            ModelResponse(
                assistant_message=final("truncated"),
                finish_reason="length",
                usage={"input_tokens": 4, "total_tokens": 8},
            )
        ]
    )
    assert await worker(store, provider).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == "failed"
    assert state.model_attempts[0].outcome == "failed"
    assert state.usage_summary().total_tokens.value == 8


def test_result_write_failure_keeps_attempt_inflight(agents):
    task, state = start(agents)
    state = reserve(agents, task, state)

    def fail_result(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE agent_model_attempts"):
            raise OperationalError("unavailable", {}, None)

    event.listen(agents.store.engine, "before_cursor_execute", fail_result)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.commit_transition(task, response(state))
    finally:
        event.remove(agents.store.engine, "before_cursor_execute", fail_result)
    assert agents.load(task) == state
    assert state.model_attempts[0].outcome == "inflight"


def test_model_reservation_keeps_room_for_bounded_usage_confirmation(
    agents, monkeypatch
):
    from test_action_reviews import fill_to_admission_limit

    import agentflow.infrastructure.persistence.agent_store as module

    monkeypatch.setattr(module, "MAX_SESSION_BYTES", 120_000)
    task, state = start(agents)
    state = fill_to_admission_limit(agents, task, reserve(agents, task, state))
    result = ModelCallResult(
        model_call_id="model-1",
        status="failed",
        usage={"total_tokens": 3, "extension": "x" * (module.MAX_USAGE_BYTES - 100)},
    )
    saved = agents.fail_agent_step(
        task,
        AgentTransition(
            snapshot=candidate(state.snapshot, status="failed"),
            model_result=result,
        ),
    )
    assert saved.model_attempts[0].raw_usage == result.usage
    assert saved.usage_summary().total_tokens.value == 3
