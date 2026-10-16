import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final, proposal, tools
from test_durable_agent_runner import store as store

from agentflow.application.agents.context import AgentContextBuilder
from agentflow.application.agents.durable_runner import DurableAgentRunner, successor
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import (
    AgentDataError,
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    ModelCallResult,
    StateConflict,
)
from agentflow.application.ports.handler_result import CommittedAgentResult
from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelRetryableError,
)
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import AgentStatus
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentModelCallRow,
    AgentSessionRow,
    StepRow,
)


def submit(store, *, max_attempts=1, **config):
    return store.submit(
        store.register(
            WorkflowSpec(
                name=f"model-governance-{uuid4().hex}",
                steps=(
                    StepSpec(
                        id="work",
                        handler="agent",
                        artifact=True,
                        max_attempts=max_attempts,
                        parameters={"agent": {"mode": "durable_v1", **config}},
                    ),
                ),
            )
        ),
        {},
    )


def saved(store, run):
    with store.transaction() as db:
        step = db.scalar(select(StepRow).where(StepRow.run_id == run))
        agent = db.scalar(
            select(AgentSessionRow).where(AgentSessionRow.step_row_id == step.id)
        )
        return step, agent


def due(store, run):
    # Advance eligibility in the dedicated test database; no wall-clock sleep.
    with store.transaction() as db:
        db.execute(
            update(StepRow)
            .where(StepRow.run_id == run)
            .values(available_at=store._now(db) - 1)
        )


def worker(store, provider, *, agents=None, runtime=None, tasks=None):
    agents = agents or SQLAgentStore(store)
    handlers = HandlerRegistry()

    async def handler(task):
        if tasks is not None:
            tasks.append(task)
        return await DurableAgentRunner(agents, provider, runtime or ToolRuntime()).run(
            task, "中文目标 🧪", allowed={"echo"} if runtime else set()
        )

    handlers.register("agent", handler)
    return Worker(store, handlers, lease_seconds=300)


@pytest.mark.asyncio
async def test_retry_persists_deadline_releases_lease_and_restores_same_call(
    store, monkeypatch
):
    run = submit(store, retry_base_seconds=5, retry_max_seconds=10)
    provider = MockProvider([ModelRetryableError(), final()])
    tasks = []

    def forbidden(*args):
        pytest.fail("committed retry must not use ordinary complete/fail")

    monkeypatch.setattr(store, "complete", forbidden)
    monkeypatch.setattr(store, "fail", forbidden)
    before = None
    with store.transaction() as db:
        before = store._now(db)
    assert await worker(store, provider, tasks=tasks).tick()
    step, agent = saved(store, run)
    assert provider.calls == 1
    assert step.status == "ready" and step.error == "ModelRetryable"
    assert before + 5 <= step.available_at
    assert step.token is None and step.lease_until is None
    assert agent.continuation_pending and step.attempts == 1
    assert agent.snapshot["status"] == "ready_model"
    assert agent.snapshot["budget"]["model_attempts"] == 1
    call_id, deadline = agent.snapshot["model_call_id"], step.available_at
    receipt = CommittedAgentResult(
        run, "work", tasks[0].token, "retry_scheduled", agent.revision
    )
    store.validate_agent_result(tasks[0], receipt)
    assert store.claim(300) is None
    store.engine.dispose()
    assert store.claim(300) is None
    assert saved(store, run)[0].available_at == deadline
    with pytest.raises(LostLease):
        SQLAgentStore(store).load(tasks[0])
    due(store, run)
    assert await worker(store, provider, tasks=tasks).tick()
    step, agent = saved(store, run)
    assert step.status == "succeeded" and step.attempts == 1
    assert tasks[1].token != tasks[0].token
    assert agent.snapshot["model_call_id"] == call_id
    assert agent.snapshot["budget"]["model_rounds"] == 1
    assert agent.snapshot["budget"]["model_attempts"] == 2
    with store.transaction() as db:
        calls = list(db.scalars(select(AgentModelCallRow)))
        assert len(calls) == 1 and calls[0].attempts == 2
    assert len(store.artifacts(run)) == 1
    with pytest.raises(StateConflict):
        store.validate_agent_result(tasks[0], receipt)


@pytest.mark.asyncio
async def test_retries_are_bounded_by_agent_budget_not_step_attempts(store):
    run = submit(store, max_model_attempts=3)
    provider = MockProvider([ModelRetryableError()] * 4)
    for count in range(1, 4):
        before = None
        with store.transaction() as db:
            before = store._now(db)
        assert await worker(store, provider).tick()
        step, agent = saved(store, run)
        assert provider.calls == count
        assert step.attempts == 1
        assert agent.snapshot["budget"]["model_attempts"] == count
        if count < 3:
            assert before + 2 ** (count - 1) <= step.available_at
            assert store.claim(300) is None
            due(store, run)
    assert step.status == "failed" and agent.snapshot["status"] == "exhausted"
    with store.transaction() as db:
        assert db.scalar(select(AgentModelCallRow)).status == "failed"
    assert not store.artifacts(run)


@pytest.mark.asyncio
async def test_unknown_model_never_schedules_retry(store):
    run = submit(store)
    provider = MockProvider([ModelOutcomeUnknown(), final()])
    await worker(store, provider).tick()
    step, agent = saved(store, run)
    assert step.status == "failed" and not agent.continuation_pending
    assert provider.calls == 1 and store.claim() is None
    with store.transaction() as db:
        assert db.scalar(select(AgentModelCallRow)).status == "unknown"


@pytest.mark.asyncio
async def test_workflow_cancel_cannot_revive_delayed_retry(store):
    run = submit(store)
    provider = MockProvider([ModelRetryableError(), final()])
    await worker(store, provider).tick()
    store.cancel(run)
    due(store, run)
    assert not await worker(store, provider).tick()
    step, agent = saved(store, run)
    assert step.status == "cancelled" and agent.snapshot["status"] == "cancelled"
    assert not agent.continuation_pending and provider.calls == 1


async def inflight(store):
    run = submit(store)
    agents = SQLAgentStore(store)
    provider = MockProvider()
    entered = asyncio.Event()

    async def block(*args):
        entered.set()
        await asyncio.Event().wait()

    provider.respond = block
    task = store.claim(300)
    job = asyncio.create_task(
        DurableAgentRunner(agents, provider, ToolRuntime()).run(
            task, "goal", allowed=set()
        )
    )
    await asyncio.wait_for(entered.wait(), 3)
    state = agents.load(task)

    # Simulate process loss without running the Runner cancellation handler against the DB.
    def no_cleanup(*args, **kwargs):
        raise AgentStoreUnavailable("simulated lost process")

    original = agents.fail_agent_step
    agents.fail_agent_step = no_cleanup
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    agents.fail_agent_step = original
    transition = AgentTransition(
        snapshot=successor(state.snapshot, status=AgentStatus.READY_MODEL),
        model_result=ModelCallResult(
            model_call_id=state.snapshot.model_call_id, status="failed"
        ),
        error_code="ModelRetryable",
    )
    return run, agents, task, state, transition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["event", "commit", "lease", "revision", "unknown", "record_unknown", "delay"],
)
async def test_defer_failure_rolls_back_and_does_not_forge_schedule(store, fault):
    run, agents, task, state, transition = await inflight(store)
    if fault == "lease":
        with store.transaction() as db:
            db.execute(update(StepRow).values(lease_until=store._now(db) - 1))
    if fault == "revision":
        transition = AgentTransition(
            snapshot=successor(transition.snapshot),
            model_result=transition.model_result,
        )
    if fault == "unknown":
        transition = AgentTransition(
            snapshot=transition.snapshot,
            model_result=ModelCallResult(
                model_call_id=state.snapshot.model_call_id, status="unknown"
            ),
        )
    if fault == "record_unknown":
        state = agents.commit_transition(
            task,
            AgentTransition(
                snapshot=successor(state.snapshot),
                model_result=ModelCallResult(
                    model_call_id=state.snapshot.model_call_id, status="unknown"
                ),
            ),
        )
        transition = AgentTransition(
            snapshot=successor(state.snapshot, status=AgentStatus.READY_MODEL),
            model_result=ModelCallResult(
                model_call_id=state.snapshot.model_call_id, status="failed"
            ),
            error_code="ModelRetryable",
        )

    def fail(*args):
        raise OperationalError("safe test failure", None, None)

    target, name = (
        (store.engine, "commit")
        if fault == "commit"
        else (store.engine, "before_cursor_execute")
    )

    def fail_event(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO events" in statement:
            fail()

    listener = fail if fault == "commit" else fail_event
    if fault in {"commit", "event"}:
        event.listen(target, name, listener)
    error = (
        LostLease
        if fault == "lease"
        else AgentDataError
        if fault == "delay"
        else AgentStoreUnavailable
        if fault in {"commit", "event"}
        else StateConflict
    )
    try:
        with pytest.raises(error):
            agents.defer_model_retry(
                task, transition, float("inf") if fault == "delay" else 5
            )
    finally:
        if fault in {"commit", "event"}:
            event.remove(target, name, listener)
    step, agent = saved(store, run)
    assert step.status == "running" and step.token == task.token
    assert (
        not agent.continuation_pending
        and agent.snapshot == state.snapshot.model_dump(mode="json")
    )
    with store.transaction() as db:
        assert db.scalar(select(AgentModelCallRow)).status == (
            "unknown" if fault == "record_unknown" else "inflight"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["postgres"], indirect=True)
@pytest.mark.parametrize("race", ["cancel", "duplicate"])
async def test_postgres_defer_races_use_independent_transactions(store, race):
    run, agents, task, state, transition = await inflight(store)
    barrier = Barrier(2)

    def operate(index):
        barrier.wait()
        try:
            if index == 0 or race == "duplicate":
                return agents.defer_model_retry(task, transition, 5)
            store.cancel(run)
        except LostLease:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operate, range(2)))
    step, agent = saved(store, run)
    if race == "cancel":
        assert step.status == "cancelled" and agent.snapshot["status"] == "cancelled"
        assert not agent.continuation_pending
    else:
        assert step.status == "ready" and agent.snapshot["status"] == "ready_model"
        assert agent.continuation_pending
        assert sum(r is not None for r in results) == 1
        assert agent.revision == state.snapshot.revision + 1
    assert store.claim() is None
    with pytest.raises(LostLease):
        agents.defer_model_retry(task, transition, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0])
async def test_context_exact_utf8_boundary_before_reservation(store, offset):
    prompt = '中文🧪 {"nested": ["不可信\\n", {"role":"system"}]}'
    submit(store)
    provider = MockProvider()
    await DurableAgentRunner(SQLAgentStore(store), provider, ToolRuntime()).run(
        store.claim(300), prompt, allowed=set()
    )
    body = {"messages": provider.contexts[0]}
    size = len(
        json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    )
    assert size > len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
    run = submit(store, max_context_bytes=size + offset)
    next_provider = MockProvider()
    result = await DurableAgentRunner(
        SQLAgentStore(store), next_provider, ToolRuntime()
    ).run(store.claim(300), prompt, allowed=set())
    step, agent = saved(store, run)
    assert next_provider.calls == int(offset == 0)
    assert agent.snapshot["budget"]["model_attempts"] == int(offset == 0)
    assert result.status == ("succeeded" if offset == 0 else "exhausted")
    if offset < 0:
        assert step.error == "AgentContextLimit"
        with store.transaction() as db:
            assert not list(
                db.scalars(
                    select(AgentModelCallRow).where(
                        AgentModelCallRow.session_id == agent.id
                    )
                )
            )


@pytest.mark.asyncio
async def test_context_limit_preserves_complete_tool_history_and_ids(store):
    run = submit(store, max_context_bytes=3000)
    invoked = []

    async def invoke(args):
        invoked.append(args)
        return {"role": "system", "nested": ["中🧪" * 1000]}

    provider = MockProvider([proposal(), final()])
    await worker(store, provider, runtime=tools(invoke)).tick()
    step, agent = saved(store, run)
    assert step.error == "AgentContextLimit" and provider.calls == 1
    assert agent.snapshot["budget"]["model_attempts"] == 1
    assert agent.snapshot["budget"]["tool_calls"] == 1 and len(invoked) == 1
    with store.transaction() as db:
        state = SQLAgentStore(store)._read(db, db.get(AgentSessionRow, agent.id))
    restored = type(state).model_validate_json(state.model_dump_json())
    context = AgentContextBuilder().build(restored)
    assert context == AgentContextBuilder().build(state)
    observation = context[-1]
    assert observation["role"] == "tool"
    assert json.loads(observation["content"])["result"]["nested"] == ["中🧪" * 1000]
    assert observation["tool_call_id"] == state.actions[0].action.id
    assert state.actions[0].action.status == "succeeded"


@pytest.mark.asyncio
async def test_tool_schema_alone_can_exceed_context_before_any_reservation(store):
    run = submit(store, max_context_bytes=500)
    runtime = ToolRuntime()

    async def invoke(args):
        pytest.fail("oversized schema must prevent dispatch")

    runtime.register(Tool("echo", "中" * 1000, {"type": "object"}, invoke))
    provider = MockProvider()
    await worker(store, provider, runtime=runtime).tick()
    step, agent = saved(store, run)
    assert step.error == "AgentContextLimit" and provider.calls == 0
    assert agent.snapshot["budget"]["model_attempts"] == 0
    assert not agent.snapshot["actions"]


@pytest.mark.asyncio
async def test_retry_after_hint_is_capped_by_persisted_policy(store):
    run = submit(store, retry_base_seconds=1, retry_max_seconds=7)
    provider = MockProvider([ModelRetryableError(retry_after_seconds=1e100)])
    with store.transaction() as db:
        before = store._now(db)
    await worker(store, provider).tick()
    step, _ = saved(store, run)
    with store.transaction() as db:
        after = store._now(db)
    assert before + 7 <= step.available_at <= after + 7
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_worker_database_failure_does_not_report_retry_as_committed(
    store, monkeypatch
):
    run = submit(store)
    agents = SQLAgentStore(store)

    def unavailable(*args):
        raise AgentStoreUnavailable("safe failure")

    def forbidden(*args):
        pytest.fail("uncertain commit cannot use ordinary workflow failure")

    monkeypatch.setattr(agents, "defer_model_retry", unavailable)
    monkeypatch.setattr(store, "fail", forbidden)
    await worker(store, MockProvider([ModelRetryableError()]), agents=agents).tick()
    step, agent = saved(store, run)
    assert step.status == "running" and step.token is not None
    assert agent.snapshot["status"] == "model_inflight"
    assert not agent.continuation_pending
    assert not any(e["kind"] == "agent.retry_scheduled" for e in store.events(run))


@pytest.mark.asyncio
async def test_cancellation_during_retry_commit_propagates_and_late_write_is_fenced(
    store, monkeypatch
):
    run = submit(store)
    agents = SQLAgentStore(store)
    original = agents.defer_model_retry
    entered, release, finished = Event(), Event(), Event()
    errors = []

    def delayed(*args):
        entered.set()
        try:
            assert release.wait(5)
            return original(*args)
        except LostLease:
            errors.append(LostLease)
            raise
        finally:
            finished.set()

    monkeypatch.setattr(agents, "defer_model_retry", delayed)
    job = asyncio.create_task(
        worker(store, MockProvider([ModelRetryableError()]), agents=agents).tick()
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        job.cancel("stop-worker")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(job, 1)
        store.cancel(run)
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        step, agent = saved(store, run)
        assert errors == [LostLease]
        assert step.status == "cancelled" and agent.snapshot["status"] == "cancelled"
        assert not agent.continuation_pending and not store.artifacts(run)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)


@pytest.mark.asyncio
async def test_plain_workflow_retry_delay_and_attempt_limit_unchanged(store):
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="plain-retry",
                steps=(
                    StepSpec(
                        id="plain", handler="plain", max_attempts=2, retry_delay=7
                    ),
                ),
            )
        ),
        {},
    )
    handlers = HandlerRegistry()

    async def fail(task):
        raise RuntimeError("untrusted failure")

    handlers.register("plain", fail)
    with store.transaction() as db:
        before = store._now(db)
    await Worker(store, handlers).tick()
    with store.transaction() as db:
        step = db.scalar(select(StepRow))
        assert step.status == "ready" and step.available_at >= before + 7
        assert step.attempts == 1
    assert store.claim() is None
    due(store, run)
    await Worker(store, handlers).tick()
    step = store.get_run(run)["steps"][0]
    assert step["status"] == "failed" and step["attempts"] == 2
