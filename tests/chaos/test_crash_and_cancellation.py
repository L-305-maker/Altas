"""故障注入：进程真的退出、执行中的协程取消、过期提交不能污染检查点。"""

import asyncio
import os
import subprocess
import sys

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import update

from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import StepRow
from agentflow.infrastructure.persistence.store import Store


@pytest.fixture
def store(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'crash.db').as_posix()}")
    store.create_schema()
    yield store
    store.close()


def test_worker_process_crash_is_recoverable(store):
    workflow_id = store.register(
        WorkflowSpec(
            name="crash", steps=(StepSpec(id="a", handler="echo", retry_delay=0),)
        )
    )
    run_id = store.submit(workflow_id, {})
    environment = dict(os.environ, AGENTFLOW_CRASH_TEST_DB=str(store.engine.url))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; from agentflow.infrastructure.persistence.store import Store; "
            "s=Store(os.environ['AGENTFLOW_CRASH_TEST_DB']); s.claim(); os._exit(13)",
        ],
        env=environment,
        timeout=10,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 13
    assert store.get_run(run_id)["steps"][0]["status"] == "running"
    # 快进租约而非让测试等待真实的 30 秒。
    with store.transaction() as session:
        session.execute(update(StepRow).values(lease_until=0))
    recovered = store.claim()
    assert recovered.attempt == 2
    assert store.complete(recovered, "recovered")
    assert store.get_run(run_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancel_running_handler_stops_cooperative_work(store):
    started, stopped = asyncio.Event(), asyncio.Event()
    registry = HandlerRegistry()

    async def handler(task):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    registry.register("wait", handler)
    run_id = store.submit(
        store.register(
            WorkflowSpec(name="cancel", steps=(StepSpec(id="a", handler="wait"),))
        ),
        {},
    )
    job = asyncio.create_task(Worker(store, registry, lease_seconds=0.15).tick())
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.to_thread(store.cancel, run_id)
    await asyncio.wait_for(job, 2)
    assert stopped.is_set()
    assert store.get_run(run_id)["steps"][0]["output"] is None


@pytest.mark.asyncio
async def test_traces_do_not_capture_error_messages_or_input(store):
    registry = HandlerRegistry()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def fail(task):
        raise RuntimeError("sensitive test content")

    registry.register("fail", fail)
    store.submit(
        store.register(
            WorkflowSpec(
                name="trace", steps=(StepSpec(id="a", handler="fail", max_attempts=1),)
            )
        ),
        {},
    )
    await Worker(store, registry, tracer=provider.get_tracer("test")).tick()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["error.type"] == "RuntimeError"
    assert spans[0].events == ()
    assert "sensitive" not in spans[0].to_json()
    provider.shutdown()
