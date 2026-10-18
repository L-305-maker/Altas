import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from test_agent_store import candidate
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store
from test_model_governance import due, submit

from agentflow.application.agents.durable_runner import (
    DurableAgentConfig,
    DurableAgentRunner,
)
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelRateLimitedError,
    ModelRetryableError,
)
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentModelAttemptRow,
    ProviderAdmissionRow,
    ProviderPermitRow,
    StepRow,
)
from agentflow.infrastructure.persistence.store import Store


def policy(**changes):
    return ProviderAdmissionPolicy(
        **{
            "provider_key": "test-provider",
            "max_concurrent_requests": 1,
            "max_requests_per_window": 20,
            "window_seconds": 60,
            "admission_retry_delay_seconds": 2,
            "permit_ttl_seconds": 120,
            "policy_version": "1",
            **changes,
        }
    )


def opened(agents):
    run = submit(agents.store, max_attempts=3)
    task = agents.store.claim(300)
    assert task.run_id == run
    state = agents.open(
        task,
        AgentSnapshot(id=uuid4().hex, budget=DurableAgentConfig().initial_budget()),
    )
    return task, state


def reservation(state):
    old = state.snapshot
    retry = bool(state.model_calls and state.model_calls[-1].status == "failed")
    return AgentTransition(
        snapshot=candidate(
            old,
            status="model_inflight",
            actions=(),
            model_call_id=old.model_call_id if retry else uuid4().hex,
            budget=old.budget.consume(model_rounds=int(not retry), model_attempts=1),
        )
    )


def worker(
    store,
    provider,
    config,
    *,
    tasks=None,
    agents=None,
    runtime=None,
    allowed=frozenset(),
):
    agents = agents or SQLAgentStore(store, admission_policy=config)
    handlers = HandlerRegistry()

    async def handler(task):
        if tasks is not None:
            tasks.append(task)
        return await DurableAgentRunner(
            agents, provider, runtime or ToolRuntime(), admission=agents
        ).run(task, "fixed public goal", allowed=set(allowed))

    handlers.register("agent", handler)
    return Worker(store, handlers, lease_seconds=300)


@pytest.mark.asyncio
async def test_admission_deferral_keeps_budget_releases_lease_and_recovers(
    store, runtime_invariants, advance_database_clock
):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    first = agents.reserve_model_attempt(task, reservation(state))
    assert first.status == "reserved" and first.permit is not None
    run = submit(store)
    provider = MockProvider([final()])
    tasks = []
    assert await worker(store, provider, policy(), tasks=tasks).tick()
    waiting = runtime_invariants(store, run)["work"]
    assert provider.calls == 0
    assert waiting.agent.snapshot.status == "ready_model"
    assert waiting.agent.snapshot.budget.model_attempts == 0
    assert waiting.agent.snapshot.budget.model_rounds == 0
    assert waiting.agent.model_attempts == () and waiting.agent.model_permits == ()
    assert (
        waiting.step["status"] == "ready"
        and waiting.step["error"] == "ProviderAdmissionDeferred"
    )
    assert waiting.step["token"] is None and waiting.step["lease_until"] is None
    assert waiting.continuation
    deadline = waiting.step["available_at"]
    store.engine.dispose()
    assert store.claim(300) is None
    assert runtime_invariants(store, run)["work"].step["available_at"] == deadline
    with pytest.raises(LostLease):
        agents.validate_model_dispatch(tasks[0], first.permit)
    advance_database_clock(121)
    assert await worker(store, provider, policy(), tasks=tasks).tick()
    saved = runtime_invariants(store, run)["work"]
    assert saved.agent.snapshot.status == "succeeded" and provider.calls == 1
    assert saved.step["attempts"] == 1
    assert tasks[1].token != tasks[0].token
    assert saved.agent.model_permits[0].status == "released"


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_three_independent_worker_engines_share_one_slot(
    store, runtime_invariants
):
    runs = [submit(store) for _ in range(3)]
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingProvider(MockProvider):
        async def respond(self, messages, tools=None):
            self.calls += 1
            entered.set()
            await release.wait()
            return await MockProvider([final()]).respond(messages, tools)

    provider = WaitingProvider()
    engines = [
        Store(store.engine.url.render_as_string(hide_password=False)) for _ in range(3)
    ]
    ticks = []
    claimed = []

    async def execute_one(engine):
        executor = worker(engine, provider, policy(), tasks=claimed)
        # SKIP LOCKED and an eligibility race can legally yield an empty tick.
        # Keep the same overall timeout and require all three persisted outcomes.
        while not await executor.tick():
            await asyncio.sleep(0)
        return True

    try:
        async with asyncio.timeout(15):
            ticks = [asyncio.create_task(execute_one(s)) for s in engines]
            await entered.wait()
            pending = set(ticks)
            for _ in range(2):
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                assert all(t.result() for t in done)
                if len(pending) == 1:
                    break
            assert len(pending) == 1 and provider.calls == 1
            assert {task.run_id for task in claimed} == set(runs)
            assert len(claimed) == 3
            checkpoints = [runtime_invariants(store, r)["work"] for r in runs]
            assert (
                sum(c.step["error"] == "ProviderAdmissionDeferred" for c in checkpoints)
                == 2
            )
            assert sum(c.agent.snapshot.budget.model_attempts for c in checkpoints) == 1
            release.set()
            await asyncio.gather(*ticks)
    finally:
        release.set()
        for task in ticks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*ticks, return_exceptions=True)
        for engine in engines:
            engine.close()


def test_permit_is_bound_to_attempt_and_original_token(store):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    transition = reservation(state)
    admitted = agents.reserve_model_attempt(task, transition)
    with pytest.raises(StateConflict):
        agents.reserve_model_attempt(task, transition)
    agents.validate_model_dispatch(task, admitted.permit)
    with pytest.raises(LostLease):
        agents.validate_model_dispatch(
            replace(task, token="old-token"), admitted.permit
        )
    with store.transaction() as db:
        assert len(list(db.scalars(select(ProviderPermitRow)))) == 1
        assert len(list(db.scalars(select(AgentModelAttemptRow)))) == 1


@pytest.mark.asyncio
async def test_sliding_window_survives_completion_and_restart_without_refund(
    store, runtime_invariants, advance_database_clock
):
    config = policy(max_requests_per_window=1)
    provider = MockProvider([final(), final()])
    run = submit(store)
    assert await worker(store, provider, config).tick()
    assert (
        runtime_invariants(store, run)["work"].agent.model_permits[0].status
        == "released"
    )
    second = submit(store)
    store.engine.dispose()
    assert await worker(store, provider, config).tick()
    assert provider.calls == 1
    assert (
        runtime_invariants(store, second)["work"].agent.snapshot.budget.model_attempts
        == 0
    )
    advance_database_clock(61)
    assert await worker(store, provider, config).tick()
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_429_shares_bounded_cooldown_even_when_attempt_budget_exhausts(
    store, runtime_invariants
):
    config = policy()
    run = submit(store, max_model_attempts=1)
    assert await worker(
        store,
        MockProvider([ModelRateLimitedError(retry_after_seconds=1000000)]),
        config,
    ).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts[0].outcome == "failed"
    assert state.model_permits[0].status == "released"
    with store.transaction() as db:
        row = db.get(ProviderAdmissionRow, config.provider_key)
        assert 0 < row.cooldown_until - store._now(db) <= 3600
    provider = MockProvider([final()])
    blocked = submit(store)
    assert await worker(store, provider, config).tick()
    assert provider.calls == 0
    assert (
        runtime_invariants(store, blocked)["work"].agent.snapshot.budget.model_attempts
        == 0
    )
    other = submit(store)
    assert await worker(store, provider, policy(provider_key="independent")).tick()
    assert store.get_run(other)["status"] == "succeeded" and provider.calls == 1


@pytest.mark.asyncio
async def test_unknown_holds_capacity_until_ttl_and_is_not_replayed(
    store, runtime_invariants, advance_database_clock
):
    config = policy(max_requests_per_window=1, window_seconds=300)
    run = submit(store)
    assert await worker(store, MockProvider([ModelOutcomeUnknown()]), config).tick()
    state = runtime_invariants(store, run)["work"].agent
    assert state.model_attempts[0].outcome == "unknown"
    assert state.model_permits[0].status == "unknown"
    second = submit(store)
    provider = MockProvider([final()])
    assert await worker(store, provider, config).tick()
    assert provider.calls == 0
    advance_database_clock(121)
    assert not await worker(store, provider, config).tick()
    assert provider.calls == 0  # TTL frees capacity, never refunds the rate window.
    advance_database_clock(180)
    assert await worker(store, provider, config).tick()
    assert provider.calls == 1
    assert (
        runtime_invariants(store, second)["work"].agent.snapshot.budget.model_attempts
        == 1
    )
    assert SQLAgentStore(store).usage_summary(state.snapshot.id).unknown_outcomes == 1


def test_policy_mismatch_and_unguarded_reservation_cannot_bypass_quota(store):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    with pytest.raises(StateConflict):
        agents.commit_transition(task, reservation(state))
    agents.reserve_model_attempt(task, reservation(state))
    wrong = SQLAgentStore(store, admission_policy=policy(max_concurrent_requests=10))
    another, ready = opened(wrong)
    with pytest.raises(StateConflict):
        wrong.reserve_model_attempt(another, reservation(ready))
    assert wrong.load(another).snapshot.budget.model_attempts == 0


def test_permit_insert_failure_rolls_back_entire_reservation(store):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO provider_permits"):
            raise OperationalError("injected failure", {}, None)

    event.listen(store.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(AgentStoreUnavailable):
            agents.reserve_model_attempt(task, reservation(state))
    finally:
        event.remove(store.engine, "before_cursor_execute", fail)
    assert agents.load(task) == state
    with store.transaction() as db:
        assert list(db.scalars(select(ProviderPermitRow))) == []
        assert list(db.scalars(select(AgentModelAttemptRow))) == []


@pytest.mark.asyncio
async def test_confirmed_failure_releases_active_slot_but_never_refunds_window(
    store, runtime_invariants
):
    config = policy(max_requests_per_window=2)
    run = submit(store)
    provider = MockProvider([ModelRetryableError(), final()])
    assert await worker(store, provider, config).tick()
    before = runtime_invariants(store, run)["work"].agent
    assert before.model_permits[0].status == "released"
    due(store, run)
    assert await worker(store, provider, config).tick()
    after = runtime_invariants(store, run)["work"].agent
    assert [p.attempt_index for p in after.model_permits] == [1, 2]
    assert len({p.model_call_id for p in after.model_permits}) == 1
    assert after.model_attempts[0] == before.model_attempts[0]
    other = submit(store)
    assert await worker(store, MockProvider([final()]), config).tick()
    assert (
        runtime_invariants(store, other)["work"].agent.snapshot.budget.model_attempts
        == 0
    )


@pytest.mark.asyncio
async def test_reservation_lost_ack_does_not_send_or_reserve_twice(
    store, monkeypatch, runtime_invariants
):
    agents = SQLAgentStore(store, admission_policy=policy())
    original = agents.reserve_model_attempt

    def lost_ack(*args):
        original(*args)
        raise AgentStoreUnavailable("commit acknowledgement lost")

    monkeypatch.setattr(agents, "reserve_model_attempt", lost_ack)
    run = submit(store, max_attempts=3)
    provider = MockProvider([final()])
    assert await worker(store, provider, policy(), agents=agents).tick()
    before = runtime_invariants(store, run)["work"].agent
    assert before.model_attempts[0].outcome == "inflight"
    assert before.model_permits[0].status == "active" and provider.calls == 0
    with store.transaction() as db:
        db.execute(update(StepRow).where(StepRow.run_id == run).values(lease_until=0))
    assert not await worker(
        store, provider, policy()
    ).tick()  # Existing Step retry delay.
    due(store, run)
    assert await worker(store, provider, policy()).tick()
    after = runtime_invariants(store, run)["work"].agent
    assert (
        after.snapshot.status == "failed"
        and after.model_attempts[0].outcome == "unknown"
    )
    assert after.model_permits[0].status == "unknown"
    assert after.model_permits[0].id == before.model_permits[0].id
    assert len(after.model_attempts) == len(after.model_permits) == 1
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("business_cancel", [False, True])
async def test_cancel_propagates_and_holds_unknown_permit(
    store, runtime_invariants, business_cancel
):
    entered = asyncio.Event()

    class WaitingProvider(MockProvider):
        async def respond(self, messages, tools=None):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    provider = WaitingProvider()
    run = submit(store)
    running = asyncio.create_task(worker(store, provider, policy()).tick())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            if business_cancel:
                await asyncio.to_thread(store.cancel, run)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    state = runtime_invariants(store, run)["work"].agent
    assert state.snapshot.status == ("cancelled" if business_cancel else "failed")
    assert state.model_attempts[0].outcome == "unknown"
    assert state.model_permits[0].status == "unknown"
    assert state.model_permits[0].released_at is None


def test_cancelled_admission_deferral_cannot_be_reclaimed(store):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    agents.reserve_model_attempt(task, reservation(state))
    delayed_task, ready = opened(agents)
    deferred = agents.reserve_model_attempt(delayed_task, reservation(ready))
    assert deferred.status == "admission_deferred"
    store.cancel(delayed_task.run_id)
    due(store, delayed_task.run_id)
    assert store.claim(300) is None
    with pytest.raises(LostLease):
        agents.reserve_model_attempt(delayed_task, reservation(ready))


def test_new_token_cannot_dispatch_or_confirm_old_permit(store):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    admitted = agents.reserve_model_attempt(task, reservation(state))
    with store.transaction() as db:
        db.execute(
            update(StepRow).where(StepRow.run_id == task.run_id).values(lease_until=0)
        )
    assert store.claim(300) is None
    due(store, task.run_id)
    new_task = store.claim(300)
    assert new_task.token != task.token
    with pytest.raises(StateConflict):
        agents.validate_model_dispatch(new_task, admitted.permit)
    with pytest.raises(LostLease):
        agents.validate_model_dispatch(task, admitted.permit)
    restored = agents.load(new_task)
    with pytest.raises(StateConflict):
        agents.commit_transition(
            new_task,
            AgentTransition(
                snapshot=candidate(restored.snapshot, status="ready_model"),
                model_result=ModelCallResult(
                    model_call_id=restored.snapshot.model_call_id, status="failed"
                ),
            ),
        )
    assert agents.load(new_task) == restored


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_admission_and_business_cancel_use_independent_transactions_without_deadlock(
    store,
):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    barrier = Barrier(2, timeout=5)

    def book():
        barrier.wait()
        try:
            return agents.reserve_model_attempt(task, reservation(state))
        except LostLease:
            return None

    def cancel():
        barrier.wait()
        store.cancel(task.run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(book), pool.submit(cancel)
        one.result(timeout=10)
        two.result(timeout=10)
    with agents._operator_transaction(state.snapshot.id) as (_, _, _, _, saved):
        assert saved.snapshot.status == "cancelled"
        assert len(saved.model_permits) <= 1
        assert all(p.status == "unknown" for p in saved.model_permits)


@pytest.mark.asyncio
async def test_bootstrap_policy_is_trusted_and_workflow_input_cannot_raise_it(
    store, runtime_invariants
):
    from agentflow.bootstrap import build_handlers
    from agentflow.config import Settings

    config = policy()
    agents = SQLAgentStore(store, admission_policy=config)
    task, state = opened(agents)
    agents.reserve_model_attempt(task, reservation(state))
    run = submit(store)
    # Ordinary inputs are deliberately ignored as quota configuration.
    from agentflow.infrastructure.persistence.models import RunRow

    with store.transaction() as db:
        db.get(RunRow, run).inputs = {
            "provider_key": "bypass",
            "max_concurrent_requests": 999999,
        }
    handlers = build_handlers(
        Settings(provider="mock", provider_admission=config), store=store
    )
    assert await Worker(store, handlers).tick()
    saved = runtime_invariants(store, run)["work"].agent
    assert saved.provider_key == config.provider_key
    assert saved.snapshot.budget.model_attempts == 0


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_repeatable_read_cannot_admit_from_stale_quota_snapshot(store, monkeypatch):
    config = policy()
    owner = SQLAgentStore(store, admission_policy=config)
    tasks = [opened(owner) for _ in range(2)]
    with store.transaction() as db:
        db.add(
            ProviderAdmissionRow(
                provider_key=config.provider_key,
                policy=config.model_dump(mode="json"),
                cooldown_until=0,
                created_at=store._now(db),
            )
        )
    connections = [
        Store(store.engine.url.render_as_string(hide_password=False)) for _ in range(2)
    ]
    barrier = Barrier(2, timeout=5)
    actors = [SQLAgentStore(s, admission_policy=config) for s in connections]
    for actor in actors:
        actor.store.engine = actor.store.engine.execution_options(
            isolation_level="REPEATABLE READ"
        )
        original = actor.admission.acquire

        def acquire(*args, original=original):
            barrier.wait()
            return original(*args)

        monkeypatch.setattr(actor.admission, "acquire", acquire)

    def book(index):
        task, state = tasks[index]
        try:
            return actors[index].reserve_model_attempt(task, reservation(state)).status
        except AgentStoreUnavailable:
            return "serialization_stopped"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(book, i) for i in range(2)]
            statuses = [r.result(timeout=10) for r in results]
        assert statuses.count("reserved") == 1
        with store.transaction() as db:
            assert len(list(db.scalars(select(ProviderPermitRow)))) == 1
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.asyncio
async def test_quota_deferral_preserves_completed_action_and_context(
    store, runtime_invariants, advance_database_clock
):
    invocations = []

    async def invoke(arguments):
        invocations.append(arguments)
        return {"value": 1}

    runtime = tools(invoke)
    provider = MockProvider([proposal(), final()])
    config = policy(max_requests_per_window=1)
    run = submit(store)
    assert await worker(
        store, provider, config, runtime=runtime, allowed={"echo"}
    ).tick()
    before = runtime_invariants(store, run)["work"].agent
    assert (
        before.snapshot.status == "ready_model"
        and before.actions[0].action.status == "succeeded"
    )
    assert len(invocations) == provider.calls == 1
    assert (
        before.snapshot.budget.model_attempts == before.snapshot.budget.tool_calls == 1
    )
    advance_database_clock(61)
    assert await worker(
        store, provider, config, runtime=runtime, allowed={"echo"}
    ).tick()
    after = runtime_invariants(store, run)["work"].agent
    assert after.snapshot.status == "succeeded" and len(invocations) == 1
    assert after.actions == before.actions
    assert sum(m["role"] == "tool" for m in provider.contexts[-1]) == 1


@pytest.mark.asyncio
async def test_admission_keeps_approval_and_unknown_tool_reconciliation(
    store, runtime_invariants
):
    from test_action_review_runner import setup

    from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision

    run, _, runtime, provider, invoked, _, _ = setup(store, unknown=True)
    agents = SQLAgentStore(store, admission_policy=policy())
    active = worker(
        store, provider, policy(), agents=agents, runtime=runtime, allowed={"write"}
    )
    assert await active.tick()
    approval = agents.list_pending_action_approvals()[0]
    waiting = runtime_invariants(store, run)["work"].agent
    assert waiting.model_permits[0].status == "released"
    agents.decide_action(
        approval.approval_id,
        ApprovalDecision(
            expected_revision=waiting.snapshot.revision,
            decision="approved",
            actor="fixture-operator",
        ),
    )
    assert await active.tick()
    unknown = runtime_invariants(store, run)["work"].agent
    assert unknown.snapshot.status == "waiting_reconciliation"
    assert (
        unknown.model_permits[0].status == "released"
    )  # Tool UNKNOWN is not a model request.
    agents.reconcile_action(
        approval.session_id,
        approval.action_id,
        ReconciliationDecision(
            request_id=uuid4().hex,
            expected_revision=unknown.snapshot.revision,
            confirmed_status="succeeded",
            result={"value": 1},
            actor="fixture-operator",
            evidence_reference="synthetic-receipt",
        ),
    )
    assert await active.tick()
    after = runtime_invariants(store, run)["work"].agent
    assert after.snapshot.status == "succeeded" and len(invoked) == 1
    assert len(after.model_permits) == len(after.model_attempts) == 2
    assert after.approvals[0].consumed_at == unknown.approvals[0].consumed_at
    assert len(store.artifacts(run)) == 1


@pytest.mark.parametrize("store", ["postgres"], indirect=True)
def test_database_lock_timeout_rolls_back_admission_without_dispatch(store):
    agents = SQLAgentStore(store, admission_policy=policy())
    task, ready = opened(agents)
    with store.transaction() as db:
        db.add(
            ProviderAdmissionRow(
                provider_key=policy().provider_key,
                policy=policy().model_dump(mode="json"),
                cooldown_until=0,
                created_at=store._now(db),
            )
        )
    url = store.engine.url.update_query_dict(
        {"options": "-c lock_timeout=100ms -c statement_timeout=2000"}
    )
    limited = Store(url.render_as_string(hide_password=False))
    try:
        with store.engine.connect() as blocker, blocker.begin():
            blocker.execute(select(ProviderAdmissionRow).with_for_update()).all()
            with pytest.raises(AgentStoreUnavailable):
                SQLAgentStore(limited, admission_policy=policy()).reserve_model_attempt(
                    task, reservation(ready)
                )
        assert agents.load(task) == ready
        with store.transaction() as db:
            assert list(db.scalars(select(ProviderPermitRow))) == []
    finally:
        limited.close()


def test_deferral_database_failure_does_not_forge_scheduling(store, monkeypatch):
    agents = SQLAgentStore(store, admission_policy=policy())
    owner, initial = opened(agents)
    agents.reserve_model_attempt(owner, reservation(initial))
    task, ready = opened(agents)
    original = store._event

    def fail(db, run_id, kind, data):
        if kind == "agent.admission_deferred":
            raise OperationalError("injected connection failure", None, None)
        original(db, run_id, kind, data)

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(AgentStoreUnavailable):
        agents.reserve_model_attempt(task, reservation(ready))
    assert agents.load(task) == ready
    with store.transaction() as db:
        step = db.scalar(select(StepRow).where(StepRow.run_id == task.run_id))
        assert step.status == "running" and step.token == task.token
