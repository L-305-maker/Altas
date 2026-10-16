"""真实子进程在持久边界退出；父进程经 Worker.tick 恢复，不伪造 Task。"""

import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import select, update

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import (
    AgentActionRow,
    AgentSessionRow,
    Base,
    StepRow,
)
from agentflow.infrastructure.persistence.store import Store

PROGRAM = """
import asyncio, os
from pathlib import Path
from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store
stage = os.environ['AGENTFLOW_CRASH_STAGE']
store = Store(os.environ['AGENTFLOW_CRASH_TEST_DB'])
agents = SQLAgentStore(store)
commit = agents.commit_transition
def crash_after_commit(task, transition):
    state = commit(task, transition)
    snapshot = state.snapshot
    if ((stage == 'model_response' and snapshot.status == 'actions_pending'
         and all(a.status == 'proposed' for a in snapshot.actions))
        or (stage == 'tool_result' and any(a.status == 'succeeded' for a in snapshot.actions))
        or (stage == 'model_reservation' and snapshot.status == 'model_inflight')):
        os._exit(13)
    return state
agents.commit_transition = crash_after_commit
defer = agents.defer_model_retry
def crash_after_retry(*args):
    state = defer(*args)
    if stage == 'retry_scheduled':
        os._exit(13)
    return state
agents.defer_model_retry = crash_after_retry
async def tool(args):
    path = Path(os.environ['AGENTFLOW_CRASH_COUNTER'])
    path.write_text(str(int(path.read_text()) + 1))
    if stage == 'tool_dispatch':
        os._exit(13)
    return {'value': 7}
tools = ToolRuntime()
tools.register(Tool('echo', 'echo', {'type': 'object'}, tool, side_effect=ToolSideEffect.READ_ONLY))
provider = MockProvider([{'role': 'assistant', 'tool_calls': [{'id': 'provider-id',
    'type': 'function', 'function': {'name': 'echo', 'arguments': '{}'}}]},
    {'role': 'assistant', 'content': 'done'}])
if stage == 'retry_scheduled':
    provider = MockProvider([ModelRetryableError()])
handlers = HandlerRegistry()
async def handler(task):
    return await DurableAgentRunner(agents, provider, tools).run(task, 'goal', allowed={'echo'})
handlers.register('agent', handler)
asyncio.run(Worker(store, handlers).tick())
"""


@pytest.fixture(params=["sqlite", "postgres"])
def database(request, tmp_path):
    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if request.param == "postgres"
        else f"sqlite:///{(tmp_path / 'crash.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database")
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    store.create_schema()
    store.close()
    yield url
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    store.close()


@pytest.mark.parametrize(
    "stage",
    [
        "model_response",
        "tool_result",
        "tool_dispatch",
        "model_reservation",
        "retry_scheduled",
    ],
)
def test_real_process_crash_and_new_worker(database, tmp_path, stage):
    store = Store(database)
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="crash-runner",
                steps=(
                    StepSpec(
                        id="work",
                        handler="agent",
                        retry_delay=0,
                        max_attempts=1 if stage == "retry_scheduled" else 3,
                        parameters={"agent": {"retry_base_seconds": 60}}
                        if stage == "retry_scheduled"
                        else {},
                    ),
                ),
            )
        ),
        {},
    )
    store.close()
    counter = tmp_path / "external-operation-count"
    counter.write_text("0")
    child = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        env=dict(
            os.environ,
            AGENTFLOW_CRASH_TEST_DB=database,
            AGENTFLOW_CRASH_STAGE=stage,
            AGENTFLOW_CRASH_COUNTER=str(counter),
        ),
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert child.returncode == 13
    # 真正断开连接后，新的 Engine 与 Worker 读取数据库中的事实。
    store = Store(database)
    try:
        with store.transaction() as db:
            ids = list(db.scalars(select(AgentActionRow.id)))
            if stage == "retry_scheduled":
                step = db.scalar(select(StepRow))
                snapshot = db.scalar(select(AgentSessionRow)).snapshot
                call_id = snapshot["model_call_id"]
                assert step.available_at > store._now(db)
                assert step.status == "ready" and step.token is None
                assert (
                    step.lease_until is None
                    and snapshot["budget"]["model_attempts"] == 1
                )
            else:
                db.execute(update(StepRow).values(lease_until=0))
        if stage == "retry_scheduled":
            assert store.claim(300) is None
            with store.transaction() as db:
                db.execute(update(StepRow).values(available_at=store._now(db) - 1))

        async def tool(args):
            counter.write_text(str(int(counter.read_text()) + 1))
            return {"value": 7}

        tools = ToolRuntime()
        tools.register(
            Tool(
                "echo",
                "echo",
                {"type": "object"},
                tool,
                side_effect=ToolSideEffect.READ_ONLY,
            )
        )
        provider = MockProvider([{"role": "assistant", "content": "done"}])
        handlers = HandlerRegistry()

        async def handler(task):
            return await DurableAgentRunner(SQLAgentStore(store), provider, tools).run(
                task, "goal", allowed={"echo"}
            )

        handlers.register("agent", handler)
        tick = asyncio.run(Worker(store, handlers).tick())
        with store.transaction() as db:
            snapshot = db.scalar(select(AgentSessionRow)).snapshot
            assert list(db.scalars(select(AgentActionRow.id))) == ids
        if stage == "retry_scheduled":
            assert tick and store.get_run(run)["status"] == "succeeded"
            assert snapshot["model_call_id"] == call_id
            assert snapshot["budget"]["model_rounds"] == 1
            assert snapshot["budget"]["model_attempts"] == 2
            assert store.get_run(run)["steps"][0]["attempts"] == 1
            assert provider.calls == 1
        elif stage == "tool_dispatch":
            assert not tick
            assert snapshot["status"] == "waiting_reconciliation"
            assert snapshot["actions"][0]["status"] == "unknown"
            assert provider.calls == 0
        elif stage == "model_reservation":
            assert tick and snapshot["status"] == "failed"
            assert snapshot["budget"]["model_attempts"] == 1
            assert provider.calls == 0
        else:
            assert tick and store.get_run(run)["status"] == "succeeded"
            assert snapshot["budget"]["model_rounds"] == 2
            assert snapshot["budget"]["tool_calls"] == 1
            assert provider.calls == 1
        assert int(counter.read_text()) == (
            0 if stage in {"model_reservation", "retry_scheduled"} else 1
        )
    finally:
        store.close()
