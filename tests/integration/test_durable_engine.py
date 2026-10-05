"""这些测试验证外部可观察行为，而非调用顺序：恢复、去重、审批和并发。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import StepRow
from agentflow.infrastructure.persistence.store import ConflictError, Store


@pytest.fixture
def store(tmp_path):
    value = Store(f"sqlite:///{(tmp_path / 'engine.db').as_posix()}")
    value.create_schema()
    yield value
    value.close()


def submit(store, *steps):
    return store.submit(
        store.register(WorkflowSpec(name="test", steps=steps)), {"value": 3}
    )


def expire(store):
    with store.transaction() as session:
        session.execute(
            update(StepRow).where(StepRow.status == "running").values(lease_until=0)
        )


def test_definition_and_submission_are_idempotent(store):
    spec = WorkflowSpec(name="test", steps=(StepSpec(id="a", handler="echo"),))
    workflow_id = store.register(spec)
    assert store.register(spec) == workflow_id
    run_id = store.submit(workflow_id, {}, "request-1")
    assert store.submit(workflow_id, {}, "request-1") == run_id
    with pytest.raises(ConflictError):
        store.submit(workflow_id, {"other": True}, "request-1")
    with pytest.raises(ConflictError):
        store.register(
            WorkflowSpec(name="test", steps=(StepSpec(id="b", handler="echo"),))
        )


def test_lease_recovery_fences_old_worker_and_checkpoints_dependencies(store):
    run_id = submit(
        store,
        StepSpec(id="a", handler="echo", retry_delay=0),
        StepSpec(id="b", handler="echo", depends_on=("a",), artifact=True),
    )
    first = store.claim()
    expire(store)
    second = store.claim()
    assert first.idempotency_key == second.idempotency_key
    assert first.token != second.token
    assert not store.complete(first, {"stale": True})
    assert store.complete(second, {"answer": 42})
    task = store.claim()
    assert task.dependencies == {"a": {"answer": 42}}
    assert store.complete(task, {"version": 1})
    assert not store.complete(task, {"version": 2})
    assert store.get_run(run_id)["status"] == "succeeded"
    assert len(store.artifacts(run_id)) == 1
    # 重新创建连接模拟进程重启：检查点仍然存在，成功步骤不会再次被领取。
    reopened = Store(store.engine.url.render_as_string(hide_password=False))
    try:
        assert reopened.get_run(run_id)["steps"][0]["output"] == {"answer": 42}
        assert reopened.claim() is None
    finally:
        reopened.close()


def test_only_one_worker_claims_a_step(store):
    submit(store, StepSpec(id="a", handler="echo"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim(), range(8)))
    assert sum(task is not None for task in claims) == 1


def test_retry_exhaustion_blocks_descendants_and_sanitizes_errors(store):
    run_id = submit(
        store,
        StepSpec(id="a", handler="echo", max_attempts=2, retry_delay=0),
        StepSpec(id="b", handler="echo", depends_on=("a",)),
    )
    for _ in range(2):
        assert store.fail(store.claim(), RuntimeError("sensitive error content"))
    result = store.get_run(run_id)
    assert result["status"] == "failed"
    assert [s["status"] for s in result["steps"]] == ["failed", "cancelled"]
    assert "sensitive" not in str(result) + str(store.events(run_id))


@pytest.mark.parametrize("approved", [True, False])
def test_approval_survives_wait_and_decision_is_idempotent(store, approved):
    run_id = submit(
        store, StepSpec(id="publish", handler="echo", requires_approval=True)
    )
    assert store.get_run(run_id)["status"] == "waiting"
    assert store.claim() is None
    approval_id = store.approvals(run_id)[0]["id"]
    store.decide(approval_id, approved, "reviewer")
    store.decide(approval_id, approved, "reviewer")
    with pytest.raises(ConflictError):
        store.decide(approval_id, not approved, "reviewer")
    assert (store.claim() is not None) == approved
    if not approved:
        assert store.get_run(run_id)["status"] == "failed"


def test_cancellation_rejects_late_results(store):
    run_id = submit(store, StepSpec(id="a", handler="echo"))
    task = store.claim()
    store.cancel(run_id)
    assert not store.heartbeat(task, 30)
    assert not store.complete(task, "late")
    assert store.claim() is None
    assert store.get_run(run_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_worker_timeout_and_checkpoint(store):
    registry = HandlerRegistry()

    async def slow(task):
        await asyncio.sleep(10)

    registry.register("slow", slow)
    run_id = submit(
        store, StepSpec(id="a", handler="slow", timeout=0.01, max_attempts=1)
    )
    assert await Worker(store, registry).tick()
    assert store.get_run(run_id)["steps"][0]["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_worker_heartbeats_during_execution(store):
    registry = HandlerRegistry()

    async def slow(task):
        await asyncio.sleep(0.2)
        return task.inputs

    registry.register("slow", slow)
    run_id = submit(store, StepSpec(id="a", handler="slow"))
    assert await Worker(store, registry, lease_seconds=0.12).tick()
    assert store.get_run(run_id)["status"] == "succeeded"
