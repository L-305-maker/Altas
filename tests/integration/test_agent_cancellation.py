"""区分协程取消、步骤超时和 Workflow 取消；真实 Task/Store 验证持久事实。"""

import asyncio
import os
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.agents.runner import AgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
    LostLease,
    StateConflict,
)
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentModelCallRow,
    AgentSessionRow,
    Base,
    StepRow,
)
from agentflow.infrastructure.persistence.store import Store


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if request.param == "postgres"
        else f"sqlite:///{(tmp_path / 'cancellation.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database")
    result = Store(url)
    Base.metadata.drop_all(result.engine)
    result.create_schema()
    yield result
    Base.metadata.drop_all(result.engine)
    result.close()


@pytest.fixture(params=["model", "tool"])
def phase(request):
    return request.param


def proposal():
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"provider-{index}",
                "type": "function",
                "function": {
                    "name": "echo",
                    "arguments": "{}",
                },
            }
            for index in range(2)
        ],
    }


async def launch(
    store,
    phase,
    *,
    worker=False,
    step_timeout=60,
    outer_timeout=None,
    lease_seconds=300,
    agent_config=None,
):
    h = SimpleNamespace(started=asyncio.Event(), calls=[], task=None)
    h.run_id = store.submit(
        store.register(
            WorkflowSpec(
                name="cancel-agent",
                steps=(
                    StepSpec(
                        id="work",
                        handler="agent",
                        artifact=True,
                        retry_delay=0,
                        timeout=step_timeout,
                        parameters={
                            "agent": {"mode": "durable_v1", **(agent_config or {})}
                        },
                    ),
                ),
            )
        ),
        {},
    )

    async def blocked():
        h.started.set()
        if outer_timeout is not None:
            h.deadline.reschedule(asyncio.get_running_loop().time() + outer_timeout)
        await asyncio.Event().wait()

    h.provider = MockProvider([proposal()])
    if phase == "model":

        async def respond(*args):
            h.calls.append("model")
            await blocked()

        h.provider.respond = respond
    h.tools = ToolRuntime()

    async def invoke(args):
        h.calls.append("tool")
        await blocked()

    h.tools.register(
        Tool(
            "echo",
            "echo",
            {"type": "object"},
            invoke,
            side_effect=ToolSideEffect.READ_ONLY,
        )
    )
    h.agents = SQLAgentStore(store)
    h.runner = DurableAgentRunner(h.agents, h.provider, h.tools)
    h.handlers = HandlerRegistry()

    async def handler(task):
        h.task = task
        async with asyncio.timeout(None) as h.deadline:
            return await h.runner.run(task, "goal", allowed={"echo"})

    h.handlers.register("agent", handler)
    if worker:
        h.job = asyncio.create_task(
            Worker(store, h.handlers, lease_seconds=lease_seconds).tick()
        )
    else:
        h.job = asyncio.create_task(handler(store.claim(300)))
    await asyncio.wait_for(h.started.wait(), 5)
    return h


def snapshot(store):
    with store.transaction() as db:
        return db.scalar(select(AgentSessionRow)).snapshot


def expire(store):
    with store.transaction() as db:
        db.execute(update(StepRow).values(lease_until=0))


def assert_unknown(store, h, phase):
    saved = snapshot(store)
    assert saved["budget"]["model_attempts"] == 1
    assert saved["budget"]["tool_calls"] == int(phase == "tool")
    step = store.get_run(h.run_id)["steps"][0]
    if phase == "model":
        assert saved["status"] == step["status"] == "failed"
        with store.transaction() as db:
            assert db.scalar(select(AgentModelCallRow)).status == "unknown"
    else:
        assert saved["status"] == "waiting_reconciliation"
        assert [a["status"] for a in saved["actions"]] == ["unknown", "proposed"]
        assert step["status"] == "waiting"
    with store.transaction() as db:
        step = db.scalar(select(StepRow))
        assert step.token is None and step.lease_until is None
    assert not store.artifacts(h.run_id)
    assert h.calls == [phase]


@pytest.mark.asyncio
async def test_direct_cancellation_propagates_and_persists_unknown(store, phase):
    h = await launch(store, phase)
    h.job.cancel("caller-stop")
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(h.job, 2)
    assert caught.value.args == ("caller-stop",)
    assert_unknown(store, h, phase)
    assert store.claim(300) is None


@pytest.mark.asyncio
async def test_outer_timeout_is_not_swallowed(store, phase):
    h = await launch(store, phase, outer_timeout=0.01)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(h.job, 2)
    assert_unknown(store, h, phase)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LostLease, StateConflict, AgentStoreUnavailable])
async def test_cleanup_failure_preserves_cancellation_and_lease_recovery(
    store, phase, monkeypatch, error
):
    h = await launch(store, phase)
    method = "fail_agent_step" if phase == "model" else "suspend_agent_step"
    original = getattr(h.agents, method)

    def failed(*args):
        raise error("safe cleanup failure")

    monkeypatch.setattr(h.agents, method, failed)
    h.job.cancel("caller-stop")
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(h.job, 2)
    assert caught.value.args == ("caller-stop",)
    assert snapshot(store)["status"] == (
        "model_inflight" if phase == "model" else "actions_pending"
    )
    assert store.get_run(h.run_id)["steps"][0]["status"] == "running"
    assert not store.artifacts(h.run_id)
    monkeypatch.setattr(h.agents, method, original)
    expire(store)
    await Worker(store, h.handlers).tick()
    assert_unknown(store, h, phase)


@pytest.mark.asyncio
async def test_lost_lease_before_cleanup_cannot_overwrite_replacement(store, phase):
    h = await launch(store, phase)
    expire(store)
    replacement = store.claim(300)
    before = snapshot(store)
    h.job.cancel("caller-stop")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(h.job, 2)
    assert snapshot(store) == before
    if phase == "model":
        assert replacement is not None and replacement.token != h.task.token
        await h.runner.run(replacement, "goal", allowed={"echo"})
    else:
        assert replacement is None
    assert_unknown(store, h, phase)


@pytest.mark.asyncio
async def test_workflow_cancellation_is_not_python_cancellation(store, phase):
    h = await launch(store, phase)
    store.cancel(h.run_id)
    before = snapshot(store)
    h.job.cancel("shutdown-after-business-cancel")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(h.job, 2)
    assert snapshot(store) == before
    assert before["status"] == "cancelled"
    assert store.get_run(h.run_id)["status"] == "cancelled"
    assert h.calls == [phase]
    assert store.claim(300) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat", [False, True])
async def test_cleanup_wait_is_bounded_and_does_not_orphan_async_tasks(
    store, phase, monkeypatch, repeat
):
    import agentflow.application.agents.durable_runner as module

    monkeypatch.setattr(module, "_CANCEL_CLEANUP_TIMEOUT", 2 if repeat else 0.05)
    h = await launch(store, phase)
    entered, release, finished = Event(), Event(), Event()
    method = "fail_agent_step" if phase == "model" else "suspend_agent_step"

    def blocked(*args):
        entered.set()
        try:
            release.wait(3)
            raise AgentStoreUnavailable("safe failure")
        finally:
            finished.set()

    monkeypatch.setattr(h.agents, method, blocked)
    tasks = asyncio.all_tasks()
    try:
        h.job.cancel("first-cancel")
        assert await asyncio.to_thread(entered.wait, 2)
        if repeat:
            assert h.job.cancel("second-cancel")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(h.job, 0.5)
        assert caught.value.args == ("first-cancel",)
        assert not (asyncio.all_tasks() - tasks)
        assert store.get_run(h.run_id)["steps"][0]["status"] == "running"
        assert not store.artifacts(h.run_id)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)


@pytest.mark.asyncio
async def test_delayed_cleanup_thread_is_fenced_after_caller_cancellation(
    store, phase, monkeypatch
):
    h = await launch(store, phase)
    entered, release, finished = Event(), Event(), Event()
    method = "fail_agent_step" if phase == "model" else "suspend_agent_step"
    original = getattr(h.agents, method)
    errors = []

    def delayed(*args):
        entered.set()
        try:
            release.wait(3)
            return original(*args)
        except Exception as error:
            errors.append(type(error))
            raise
        finally:
            finished.set()

    monkeypatch.setattr(h.agents, method, delayed)
    try:
        h.job.cancel("caller-stop")
        assert await asyncio.to_thread(entered.wait, 2)
        expire(store)
        replacement = store.claim(300)
        before = snapshot(store)
        assert h.job.cancel("cancel-cleanup-wait")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(h.job, 1)
        assert caught.value.args == ("caller-stop",)
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        assert errors == [LostLease]
        assert snapshot(store) == before
        monkeypatch.setattr(h.agents, method, original)
        if phase == "model":
            await h.runner.run(replacement, "goal", allowed={"echo"})
        assert_unknown(store, h, phase)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_worker_step_timeout_does_not_double_commit_or_fail_unknown_tools(
    store, phase, monkeypatch, cleanup_fails
):
    h = await launch(store, phase, worker=True, step_timeout=1)
    if cleanup_fails:
        method = "fail_agent_step" if phase == "model" else "suspend_agent_step"

        def failed(*args):
            raise AgentStoreUnavailable("safe failure")

        original = getattr(h.agents, method)
        monkeypatch.setattr(h.agents, method, failed)
    assert await asyncio.wait_for(h.job, 3)
    if cleanup_fails:
        assert store.get_run(h.run_id)["steps"][0]["status"] == "running"
        monkeypatch.setattr(h.agents, method, original)
        expire(store)
        await Worker(store, h.handlers).tick()
    assert_unknown(store, h, phase)
    kinds = [event["kind"] for event in store.events(h.run_id)]
    assert kinds.count("step.failed" if phase == "model" else "step.waiting") == 1
    assert "step.succeeded" not in kinds


@pytest.mark.asyncio
async def test_worker_shutdown_propagates_cancellation(store, phase):
    h = await launch(store, phase, worker=True)
    h.job.cancel("worker-stop")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(h.job, 2)
    assert_unknown(store, h, phase)


@pytest.mark.asyncio
async def test_workflow_cancel_stops_worker_via_heartbeat(store, phase):
    h = await launch(store, phase, worker=True, lease_seconds=3)
    store.cancel(h.run_id)
    assert await asyncio.wait_for(h.job, 5)
    assert snapshot(store)["status"] == "cancelled"
    assert store.get_run(h.run_id)["status"] == "cancelled"
    assert h.calls == [phase]
    assert not store.artifacts(h.run_id)


@pytest.mark.asyncio
async def test_plain_handler_timeout_keeps_normal_retry_semantics(store):
    run_id = store.submit(
        store.register(
            WorkflowSpec(
                name="plain-timeout",
                steps=(
                    StepSpec(
                        id="work",
                        handler="plain",
                        timeout=0.01,
                        retry_delay=0,
                        max_attempts=2,
                    ),
                ),
            )
        ),
        {},
    )
    handlers = HandlerRegistry()

    async def plain(task):
        await asyncio.Event().wait()

    handlers.register("plain", plain)
    worker = Worker(store, handlers)
    await worker.tick()
    assert store.get_run(run_id)["steps"][0]["status"] == "ready"
    await worker.tick()
    step = store.get_run(run_id)["steps"][0]
    assert step["status"] == "failed" and step["attempts"] == 2
    assert step["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_worker_timeout_after_atomic_completion_does_not_duplicate_artifact(
    store, monkeypatch
):
    run_id = store.submit(
        store.register(
            WorkflowSpec(
                name="completion-race",
                steps=(StepSpec(id="work", handler="agent", artifact=True, timeout=1),),
            )
        ),
        {},
    )
    agents = SQLAgentStore(store)
    complete = agents.complete_agent_step
    entered, release, finished = Event(), Event(), Event()

    def commit_then_block(*args):
        result = complete(*args)
        entered.set()
        try:
            release.wait(5)
            return result
        finally:
            finished.set()

    monkeypatch.setattr(agents, "complete_agent_step", commit_then_block)
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(agents, MockProvider(), ToolRuntime()).run(
            task, "goal", allowed=set()
        )

    handlers.register("agent", handler)
    job = asyncio.create_task(Worker(store, handlers).tick())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert await asyncio.wait_for(job, 3)
        assert snapshot(store)["status"] == "succeeded"
        assert store.get_run(run_id)["status"] == "succeeded"
        assert len(store.artifacts(run_id)) == 1
        assert [e["kind"] for e in store.events(run_id)].count("step.succeeded") == 1
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["model", "tool"])
async def test_legacy_runner_still_propagates_cancellation(phase):
    started = asyncio.Event()

    async def block(*args):
        started.set()
        await asyncio.Event().wait()

    provider = MockProvider([proposal()])
    if phase == "model":
        provider.complete = block
    runtime = ToolRuntime()
    runtime.register(Tool("echo", "echo", {"type": "object"}, block))
    job = asyncio.create_task(
        AgentRunner(provider, runtime).run("goal", allowed={"echo"})
    )
    await asyncio.wait_for(started.wait(), 2)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
