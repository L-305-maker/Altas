import asyncio
import os

import pytest
from sqlalchemy import select

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import (
    AgentStoreUnavailable,
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
            side_effect=ToolSideEffect.READ_ONLY,
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

    task = store.claim(300)
    await DurableAgentRunner(SQLAgentStore(store), provider, tools(invoke)).run(
        task, "goal", allowed={"echo"}
    )
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
@pytest.mark.parametrize("arguments", ['{"value":"bad"}', "{broken"])
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
async def test_last_model_round_never_dispatches_tool(store):
    submit(store, max_model_rounds=1)
    invoked = []

    async def invoke(args):
        invoked.append(args)

    await DurableAgentRunner(
        SQLAgentStore(store), MockProvider([proposal()]), tools(invoke)
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
async def test_unapproved_or_unauthorized_tools_are_denied(store, policy):
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
    assert store.get_run(run)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_retry_budget_is_bounded(store):
    run = submit(store, max_model_attempts=2)
    provider = MockProvider([ModelRetryableError()] * 3)
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

    async def invoke(args):
        if kind == "tool_timeout":
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
            await asyncio.sleep(1)

        provider.respond = respond
    runner = DurableAgentRunner(SQLAgentStore(store), provider, tools(invoke))
    handlers = HandlerRegistry()

    async def handler(task):
        async with asyncio.timeout(0.1 if "timeout" in kind else 10):
            return await runner.run(task, "goal", allowed={"echo"})

    handlers.register("agent", handler)
    attempts = []
    monkeypatch.setattr(store, "complete", lambda *args: attempts.append("complete"))
    monkeypatch.setattr(store, "fail", lambda *args: attempts.append("fail"))
    assert await Worker(store, handlers).tick()
    assert attempts == []
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
