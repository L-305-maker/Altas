import asyncio
import os

import pytest
from sqlalchemy import select, update

from agentflow.application.agents.durable_runner import DurableAgentRunner, successor
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import (
    AgentDataError,
    AgentStoreUnavailable,
    AgentTransition,
    LostLease,
    StateConflict,
)
from agentflow.application.ports.handler_result import CommittedAgentResult
from agentflow.application.ports.model_provider import (
    ModelOutcomeUnknown,
    ModelResponse,
    ModelRetryableError,
)
from agentflow.application.tools.runtime import Tool, ToolConfirmedFailure, ToolRuntime
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
        else f"sqlite:///{(tmp_path / 'runner.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database")
    result = Store(url)
    Base.metadata.drop_all(result.engine)
    result.create_schema()
    yield result
    Base.metadata.drop_all(result.engine)
    result.close()


def submit(store, **agent):
    return store.submit(
        store.register(
            WorkflowSpec(
                name="durable-runner",
                steps=(
                    StepSpec(
                        id="work",
                        handler="agent",
                        artifact=True,
                        retry_delay=0,
                        parameters={"agent": {"mode": "durable_v1", **agent}},
                    ),
                ),
            )
        ),
        {},
    )


def final(text="done"):
    return {"role": "assistant", "content": text}


def proposal(arguments='{"value": 1}', count=1):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"provider-{i}",
                "type": "function",
                "function": {"name": "echo", "arguments": arguments},
            }
            for i in range(count)
        ],
    }


def tools(invoke, **changes):
    runtime = ToolRuntime()
    runtime.register(
        Tool(
            "echo",
            "echo",
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            invoke,
            side_effect=changes.pop("side_effect", ToolSideEffect.READ_ONLY),
            **changes,
        )
    )
    return runtime


def persisted(store):
    with store.transaction() as db:
        row = db.scalar(select(AgentSessionRow))
        return row.snapshot


@pytest.mark.asyncio
async def test_direct_final_atomic_success(store):
    run = submit(store)
    task = store.claim(300)
    result = await DurableAgentRunner(
        SQLAgentStore(store), MockProvider(), ToolRuntime()
    ).run(task, "goal", allowed=set())
    assert result.status == "succeeded"
    assert store.get_run(run)["status"] == "succeeded"
    assert persisted(store)["budget"]["model_attempts"] == 1


@pytest.mark.asyncio
async def test_ordered_tools_history_and_budget(store):
    run = submit(store)
    provider = MockProvider([proposal(count=2), proposal(), final()])
    invoked = []

    async def invoke(arguments):
        invoked.append(arguments)
        return {"value": arguments["value"]}

    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            SQLAgentStore(store), provider, tools(invoke)
        ).run(task, "goal", allowed={"echo"})

    handlers.register("agent", handler)
    await Worker(store, handlers).tick()
    assert store.get_run(run)["status"] == "succeeded"
    assert len(invoked) == 3
    budget = persisted(store)["budget"]
    assert (budget["model_rounds"], budget["model_attempts"], budget["tool_calls"]) == (
        3,
        3,
        3,
    )
    calls = [
        c["id"]
        for m in provider.contexts[-1]
        if m["role"] == "assistant"
        for c in m.get("tool_calls", [])
    ]
    assert len(set(calls)) == 3
    assert calls == [
        m["tool_call_id"] for m in provider.contexts[-1] if m["role"] == "tool"
    ]


@pytest.mark.asyncio
async def test_confirmed_model_failure_retries_same_logical_id(store):
    submit(store)
    provider = MockProvider([ModelRetryableError(), final()])
    result = await DurableAgentRunner(
        SQLAgentStore(store), provider, ToolRuntime()
    ).run(store.claim(300), "goal", allowed=set())
    assert result.status == "retry_scheduled" and provider.calls == 1
    with store.transaction() as db:
        db.execute(update(StepRow).values(available_at=store._now(db) - 1))
    await DurableAgentRunner(SQLAgentStore(store), provider, ToolRuntime()).run(
        store.claim(300), "goal", allowed=set()
    )
    with store.transaction() as db:
        calls = list(db.scalars(select(AgentModelCallRow)))
        assert len(calls) == 1 and calls[0].attempts == 2
    assert persisted(store)["budget"]["model_rounds"] == 1


@pytest.mark.asyncio
async def test_unknown_model_fails_closed_without_retry(store):
    run = submit(store)
    provider = MockProvider([ModelOutcomeUnknown(), final()])
    await DurableAgentRunner(SQLAgentStore(store), provider, ToolRuntime()).run(
        store.claim(300), "goal", allowed=set()
    )
    assert store.get_run(run)["status"] == "failed"
    assert provider.calls == 1
    with store.transaction() as db:
        assert db.scalar(select(AgentModelCallRow)).status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    ['{"value":"bad"}', "{broken", '{"value": 1, "value": 2}', '{"value": NaN}', "[]"],
)
async def test_argument_error_can_be_corrected_without_dispatch(store, arguments):
    run = submit(store)
    provider = MockProvider([proposal(arguments), proposal(), final()])
    invoked = []

    async def invoke(args):
        invoked.append(args)
        return "ok"

    await DurableAgentRunner(SQLAgentStore(store), provider, tools(invoke)).run(
        store.claim(300), "goal", allowed={"echo"}
    )
    assert store.get_run(run)["status"] == "succeeded"
    assert len(invoked) == 1
    assert '"status":"denied"' in provider.contexts[1][-1]["content"]


@pytest.mark.asyncio
async def test_unknown_tool_suspends_and_does_not_retry(store):
    run = submit(store)

    async def invoke(args):
        raise TimeoutError("private error")

    provider = MockProvider([proposal(), final()])
    await DurableAgentRunner(SQLAgentStore(store), provider, tools(invoke)).run(
        store.claim(300), "goal", allowed={"echo"}
    )
    assert persisted(store)["status"] == "waiting_reconciliation"
    assert store.get_run(run)["steps"][0]["status"] == "waiting"
    assert provider.calls == 1
    assert store.claim(300) is None


@pytest.mark.asyncio
async def test_confirmed_tool_failure_is_feedback(store):
    run = submit(store)

    async def invoke(args):
        raise ToolConfirmedFailure("RemoteRejected")

    provider = MockProvider([proposal(), final()])
    await DurableAgentRunner(SQLAgentStore(store), provider, tools(invoke)).run(
        store.claim(300), "goal", allowed={"echo"}
    )
    assert store.get_run(run)["status"] == "succeeded"
    assert "RemoteRejected" in provider.contexts[-1][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit", [{"max_model_rounds": 1}, {"max_model_attempts": 1}, {"max_tool_calls": 0}]
)
async def test_last_model_round_never_dispatches_tool(store, limit):
    submit(store, **limit)
    invoked = []

    async def invoke(args):
        invoked.append(args)

    await DurableAgentRunner(
        SQLAgentStore(store),
        MockProvider([proposal()]),
        tools(invoke, side_effect=ToolSideEffect.IDEMPOTENT),
    ).run(store.claim(300), "goal", allowed={"echo"})
    assert not invoked
    assert persisted(store)["status"] == "exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,reason", [("", "stop"), ("partial", "length"), ("   ", "stop")]
)
async def test_invalid_final_never_succeeds(store, text, reason):
    run = submit(store)
    provider = MockProvider(
        [ModelResponse(assistant_message=final(text), finish_reason=reason)]
    )
    await DurableAgentRunner(SQLAgentStore(store), provider, ToolRuntime()).run(
        store.claim(300), "goal", allowed=set()
    )
    assert store.get_run(run)["status"] == "failed"
    assert store.get_run(run)["steps"][0]["error"] == "ModelInvalidFinal"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["approval", "not_allowed", "unsafe"])
async def test_protected_tools_wait_and_unauthorized_tools_are_denied(store, policy):
    run = submit(store)
    invoked = []

    async def invoke(args):
        invoked.append(args)

    runtime = ToolRuntime()
    runtime.register(
        Tool(
            "echo",
            "echo",
            {"type": "object"},
            invoke,
            requires_approval=policy == "approval",
            side_effect=ToolSideEffect.UNSAFE
            if policy == "unsafe"
            else ToolSideEffect.READ_ONLY,
        )
    )
    provider = MockProvider([proposal(), final()])
    await DurableAgentRunner(SQLAgentStore(store), provider, runtime).run(
        store.claim(300), "goal", allowed=set() if policy == "not_allowed" else {"echo"}
    )
    assert not invoked
    assert persisted(store)["budget"]["tool_calls"] == 0
    assert store.get_run(run)["status"] == (
        "succeeded" if policy == "not_allowed" else "waiting"
    )
    if policy != "not_allowed":
        assert len(SQLAgentStore(store).list_pending_action_approvals()) == 1


@pytest.mark.asyncio
async def test_retry_budget_is_bounded(store):
    run = submit(store, max_model_attempts=2)
    provider = MockProvider([ModelRetryableError()] * 3)
    await DurableAgentRunner(SQLAgentStore(store), provider, ToolRuntime()).run(
        store.claim(300), "goal", allowed=set()
    )
    assert provider.calls == 1
    with store.transaction() as db:
        db.execute(update(StepRow).values(available_at=store._now(db) - 1))
    await DurableAgentRunner(SQLAgentStore(store), provider, ToolRuntime()).run(
        store.claim(300), "goal", allowed=set()
    )
    assert provider.calls == 2
    assert persisted(store)["budget"]["model_attempts"] == 2
    assert persisted(store)["status"] == "exhausted"
    assert store.get_run(run)["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ["success", "unknown_tool", "unknown_model", "tool_timeout", "model_timeout"],
)
async def test_worker_honors_durable_result_without_double_commit(
    store, monkeypatch, kind
):
    submit(store)
    deadline = None

    async def invoke(args):
        if kind == "tool_timeout":
            deadline.reschedule(asyncio.get_running_loop().time() + 0.1)
            await asyncio.sleep(1)
        raise TimeoutError("private")

    provider = MockProvider(
        [proposal()]
        if "tool" in kind
        else [ModelOutcomeUnknown()]
        if kind == "unknown_model"
        else [final()]
    )
    if kind == "model_timeout":

        async def respond(*args):
            deadline.reschedule(asyncio.get_running_loop().time() + 0.1)
            await asyncio.sleep(1)

        provider.respond = respond
    runner = DurableAgentRunner(SQLAgentStore(store), provider, tools(invoke))
    handlers = HandlerRegistry()

    async def handler(task):
        nonlocal deadline
        async with asyncio.timeout(10) as deadline:
            return await runner.run(task, "goal", allowed={"echo"})

    handlers.register("agent", handler)
    attempts = []
    monkeypatch.setattr(store, "complete", lambda *args: attempts.append("complete"))
    fail = store.fail

    def timeout_probe(task, error):
        assert isinstance(error, TimeoutError)
        before = store.get_run(task.run_id)
        assert fail(task, error) is False
        assert store.get_run(task.run_id) == before
        attempts.append("timeout_probe")

    monkeypatch.setattr(store, "fail", timeout_probe)
    assert await Worker(store, handlers).tick()
    assert attempts == (["timeout_probe"] if "timeout" in kind else [])
    assert persisted(store)["status"] == (
        "waiting_reconciliation"
        if "tool" in kind
        else "failed"
        if "model" in kind
        else "succeeded"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [LostLease, StateConflict, AgentStoreUnavailable])
async def test_worker_does_not_fail_uncertain_or_lost_execution(
    store, monkeypatch, error
):
    submit(store)
    handlers = HandlerRegistry()

    async def handler(task):
        raise error()

    handlers.register("agent", handler)
    failures = []
    monkeypatch.setattr(store, "fail", lambda *args: failures.append(args))
    await Worker(store, handlers).tick()
    assert not failures


@pytest.mark.asyncio
async def test_worker_rejects_result_for_another_task(store):
    run = submit(store)
    handlers = HandlerRegistry()

    async def handler(task):
        return CommittedAgentResult(
            "wrong-run", task.step_id, task.token, "succeeded", 1
        )

    handlers.register("agent", handler)
    await Worker(store, handlers).tick()
    assert store.get_run(run)["steps"][0]["output"] is None
    assert store.get_run(run)["steps"][0]["error"] == "ValueError"


@pytest.mark.asyncio
async def test_worker_rejects_uncommitted_marker(store):
    run = submit(store)
    handlers = HandlerRegistry()

    async def handler(task):
        return CommittedAgentResult(
            task.run_id, task.step_id, task.token, "succeeded", 1
        )

    handlers.register("agent", handler)
    await Worker(store, handlers).tick()
    assert store.get_run(run)["steps"][0]["error"] == "ValueError"


@pytest.mark.asyncio
async def test_cancellation_after_reservation_prevents_tool_dispatch(
    store, monkeypatch
):
    run = submit(store)
    agents = SQLAgentStore(store)
    commit = agents.commit_transition

    def cancel_after_reservation(task, transition):
        saved = commit(task, transition)
        if any(a.status == "inflight" for a in saved.snapshot.actions):
            store.cancel(run)
        return saved

    monkeypatch.setattr(agents, "commit_transition", cancel_after_reservation)
    invoked = []

    async def invoke(args):
        invoked.append(args)

    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            agents, MockProvider([proposal()]), tools(invoke)
        ).run(task, "goal", allowed={"echo"})

    handlers.register("agent", handler)
    await Worker(store, handlers).tick()
    assert not invoked
    assert persisted(store)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_replacement_worker_fences_late_model_response(store):
    run = submit(store)
    started, release = asyncio.Event(), asyncio.Event()

    async def respond(*args):
        started.set()
        await release.wait()
        return ModelResponse(assistant_message=final("late"), finish_reason="stop")

    provider = MockProvider()
    provider.respond = respond
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            SQLAgentStore(store), provider, ToolRuntime()
        ).run(task, "goal", allowed=set())

    handlers.register("agent", handler)
    old = asyncio.create_task(Worker(store, handlers, lease_seconds=300).tick())
    try:
        await asyncio.wait_for(started.wait(), 5)
        with store.transaction() as db:
            db.execute(update(StepRow).values(lease_until=0))
        # 新 Worker 真实领取，在恢复中把未知模型结果终止，不再次请求。
        await Worker(store, handlers, lease_seconds=300).tick()
        release.set()
        await asyncio.wait_for(old, 5)
        assert store.get_run(run)["steps"][0]["output"] is None
        assert persisted(store)["status"] == "failed"
    finally:
        release.set()
        await old


@pytest.mark.asyncio
async def test_replacement_worker_suspends_unknown_tool_and_fences_late_result(store):
    run = submit(store)
    started, release = asyncio.Event(), asyncio.Event()
    invoked = []

    async def invoke(args):
        invoked.append(args)
        started.set()
        await release.wait()
        return "late result"

    provider = MockProvider([proposal(), final()])
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            SQLAgentStore(store), provider, tools(invoke)
        ).run(task, "goal", allowed={"echo"})

    handlers.register("agent", handler)
    old = asyncio.create_task(Worker(store, handlers, lease_seconds=300).tick())
    try:
        await asyncio.wait_for(started.wait(), 5)
        with store.transaction() as db:
            db.execute(update(StepRow).values(lease_until=0))
        assert not await Worker(store, handlers).tick()
        release.set()
        await asyncio.wait_for(old, 5)
        assert len(invoked) == provider.calls == 1
        assert persisted(store)["status"] == "waiting_reconciliation"
        assert persisted(store)["actions"][0]["status"] == "unknown"
        assert store.get_run(run)["steps"][0]["status"] == "waiting"
    finally:
        release.set()
        await old


@pytest.mark.asyncio
async def test_recovery_validates_message_mapping_before_any_dispatch(
    store, monkeypatch
):
    submit(store)
    agents = SQLAgentStore(store)
    commit = agents.commit_transition

    class Crash(BaseException):
        pass

    def crash_after_proposals(task, transition):
        saved = commit(task, transition)
        if saved.snapshot.status == "actions_pending":
            raise Crash
        return saved

    monkeypatch.setattr(agents, "commit_transition", crash_after_proposals)

    async def invoke(args):
        pytest.fail("unmapped proposals cannot be dispatched")

    task = store.claim(300)
    with pytest.raises(Crash):
        await DurableAgentRunner(agents, MockProvider([proposal()]), tools(invoke)).run(
            task, "goal", allowed={"echo"}
        )
    from agentflow.infrastructure.persistence.models import AgentMessageRow

    with store.transaction() as db:
        message = db.scalar(
            select(AgentMessageRow).where(AgentMessageRow.role == "assistant")
        )
        message.payload = {"action_ids": message.payload["action_ids"]}
    monkeypatch.setattr(agents, "commit_transition", commit)
    with pytest.raises(StateConflict):
        await DurableAgentRunner(agents, MockProvider([final()]), tools(invoke)).run(
            task, "goal", allowed={"echo"}
        )


@pytest.mark.asyncio
async def test_all_restored_inflight_actions_become_unknown_atomically(
    store, monkeypatch
):
    submit(store)
    agents = SQLAgentStore(store)
    commit = agents.commit_transition

    class Crash(BaseException):
        pass

    def crash_after_proposals(task, transition):
        saved = commit(task, transition)
        if saved.snapshot.status == "actions_pending":
            raise Crash
        return saved

    monkeypatch.setattr(agents, "commit_transition", crash_after_proposals)

    async def invoke(args):
        pytest.fail("restored inflight action cannot be dispatched again")

    task = store.claim(300)
    with pytest.raises(Crash):
        await DurableAgentRunner(
            agents, MockProvider([proposal(count=2)]), tools(invoke)
        ).run(task, "goal", allowed={"echo"})
    monkeypatch.setattr(agents, "commit_transition", commit)
    state = agents.load(task)
    agents.commit_transition(
        task,
        AgentTransition(
            snapshot=successor(
                state.snapshot,
                actions=tuple(a.transition("inflight") for a in state.snapshot.actions),
                budget=state.snapshot.budget.consume(tool_calls=2),
            )
        ),
    )
    result = await DurableAgentRunner(agents, MockProvider(), tools(invoke)).run(
        task, "goal", allowed={"echo"}
    )
    assert result.status == "waiting_reconciliation"
    assert [a["status"] for a in persisted(store)["actions"]] == ["unknown", "unknown"]


@pytest.mark.asyncio
async def test_result_storage_limit_suspends_instead_of_losing_outcome(
    store, monkeypatch
):
    submit(store)
    agents = SQLAgentStore(store)
    commit = agents.commit_transition

    def storage_limit(task, transition):
        if any(a.status == "succeeded" for a in transition.snapshot.actions):
            raise AgentDataError("agent payload exceeds storage limit")
        return commit(task, transition)

    monkeypatch.setattr(agents, "commit_transition", storage_limit)

    async def invoke(args):
        return {"value": 1}

    result = await DurableAgentRunner(
        agents, MockProvider([proposal()]), tools(invoke)
    ).run(store.claim(300), "goal", allowed={"echo"})
    assert result.status == "waiting_reconciliation"
    assert persisted(store)["actions"][0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_tool_receives_persisted_action_identity_and_key(store):
    from agentflow.infrastructure.persistence.models import AgentActionRow

    submit(store)
    observed = []

    async def fallback(args):
        pytest.fail("durable invocation must receive action context")

    async def invoke(args, invocation):
        with store.transaction() as db:
            saved = db.scalar(select(AgentActionRow))
            assert saved.status == "inflight"
            assert saved.id == invocation.action_id
            assert saved.idempotency_key == invocation.idempotency_key
        observed.append(invocation)
        return "ok"

    runtime = tools(
        fallback, invoke_durable=invoke, side_effect=ToolSideEffect.IDEMPOTENT
    )
    await DurableAgentRunner(
        SQLAgentStore(store), MockProvider([proposal(), final()]), runtime
    ).run(store.claim(300), "goal", allowed={"echo"})
    assert len(observed) == 1
