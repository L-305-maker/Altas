"""专用 PostgreSQL 的跨 Worker / 多 Step 闭环；连接超时限制真实锁等待。"""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event, update

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import StateConflict
from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import Base, StepRow
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics


@pytest.fixture
def workers(tmp_path):
    url = os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires a dedicated real PostgreSQL database")
    metrics = RuntimeMetrics()
    stores = [Store(url, metrics=metrics), Store(url, metrics=metrics)]

    def limits(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '5s'")
            cursor.execute("SET lock_timeout = '2s'")
        connection.commit()

    for store in stores:
        event.listen(store.engine, "connect", limits)
    Base.metadata.drop_all(stores[0].engine)
    stores[0].create_schema()
    yield stores
    Base.metadata.drop_all(stores[0].engine)
    for store in stores:
        store.close()


def tool_proposal():
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "provider-id",
                "type": "function",
                "function": {"name": "write", "arguments": '{"value":1}'},
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish",
    [
        "complete",
        "cancel_after_approval",
        "cancel_due_retry",
        "cancel_reconciliation_race",
    ],
)
async def test_two_workers_review_reconcile_delay_and_dag(
    workers, runtime_invariants, finish
):
    async with asyncio.timeout(25):
        one, two = workers
        run = one.submit(
            one.register(
                WorkflowSpec(
                    name="runtime-chain",
                    steps=(
                        StepSpec(
                            id="a",
                            handler="mixed",
                            max_attempts=1,
                            artifact=True,
                            parameters={"agent": {"retry_base_seconds": 60}},
                        ),
                        StepSpec(
                            id="b",
                            handler="mixed",
                            max_attempts=1,
                            artifact=True,
                            parameters={"agent": {"retry_base_seconds": 60}},
                        ),
                        StepSpec(
                            id="after",
                            handler="plain",
                            depends_on=("a", "b"),
                            artifact=True,
                        ),
                    ),
                )
            ),
            {},
        )
        control, tasks, plain_calls, invocations = [], [], [], []
        provider = MockProvider(
            [
                tool_proposal(),
                ModelRetryableError(),
                {"role": "assistant", "content": "done"},
            ]
        )
        tools = ToolRuntime()

        async def uncertain(args):
            invocations.append(args)
            raise TimeoutError("private tool outcome")

        tools.register(
            Tool(
                "write", "write", {"type": "object"}, uncertain, requires_approval=True
            )
        )

        def build(store):
            handlers = HandlerRegistry()

            async def plain(task):
                plain_calls.append((task.run_id, task.step_id))
                return {"ok": True}

            async def mixed(task):
                if not control:
                    control.append(task.step_id)
                if task.step_id != control[0]:
                    return await plain(task)
                tasks.append(task)
                return await DurableAgentRunner(
                    SQLAgentStore(store), provider, tools
                ).run(task, "goal", allowed={"write"})

            handlers.register("mixed", mixed)
            handlers.register("plain", plain)
            return Worker(store, handlers, lease_seconds=300)

        first, second = build(one), build(two)
        assert await first.tick()
        key = control[0]
        waiting = runtime_invariants(one, run)[key]
        assert waiting.agent.snapshot.status == "waiting_approval"
        statuses = {s["id"]: s["status"] for s in one.get_run(run)["steps"]}
        other = ({"a", "b"} - {key}).pop()
        assert statuses[other] == "ready" and statuses["after"] == "pending"
        assert await second.tick()  # 同 Run 的另一个就绪 Step 不受人工等待阻塞。
        assert (run, other) in plain_calls
        runtime_invariants(two, run)

        independent = one.submit(
            one.register(
                WorkflowSpec(
                    name="independent", steps=(StepSpec(id="free", handler="plain"),)
                )
            ),
            {},
        )
        assert await second.tick()
        isolated = two.get_run(independent)
        approval = waiting.agent.approvals[0]
        agents = SQLAgentStore(one)
        agents.decide_action(
            approval.approval_id,
            ApprovalDecision(
                expected_revision=waiting.agent.snapshot.revision,
                decision="approved",
                actor="operator",
            ),
        )
        runtime_invariants(one, run)
        if finish == "cancel_after_approval":
            two.cancel(run)
            runtime_invariants(one, run)
            assert not await first.tick()
            assert not invocations
        else:
            assert await second.tick()
            unknown = runtime_invariants(one, run)[key]
            assert unknown.agent.snapshot.status == "waiting_reconciliation"
            assert unknown.agent.approvals[0].consumed_at is not None
            assert len(invocations) == 1 and provider.calls == 1
            assert not await first.tick()
            assert not any(step == "after" for r, step in plain_calls if r == run)
            action = unknown.agent.actions[0].action
            if finish == "cancel_reconciliation_race":
                barrier = Barrier(3)

                def race_review(index):
                    barrier.wait(timeout=3)
                    try:
                        if index == 0:
                            return agents.reconcile_action(
                                unknown.agent.snapshot.id,
                                action.id,
                                ReconciliationDecision(
                                    request_id="race-evidence",
                                    expected_revision=unknown.agent.snapshot.revision,
                                    confirmed_status="succeeded",
                                    result={"value": 1},
                                    evidence_reference="external-race-record",
                                    actor="operator",
                                ),
                            )
                        if index == 1:
                            two.cancel(run)
                        else:
                            return one.claim(300)
                    except StateConflict:
                        return None

                with ThreadPoolExecutor(max_workers=3) as pool:
                    futures = [pool.submit(race_review, i) for i in range(3)]
                    results = [f.result(timeout=10) for f in futures]
                checked = runtime_invariants(two, run)[key]
                assert checked.run_status == "cancelled"
                assert not any(
                    s["id"] == "after" and s["status"] == "succeeded"
                    for s in one.get_run(run)["steps"]
                )
                assert two.get_run(independent) == isolated and len(invocations) == 1
                for task in tasks + ([results[2]] if results[2] is not None else []):
                    runtime_invariants.fenced(agents, task)
                assert not await first.tick()
                return
            agents.reconcile_action(
                unknown.agent.snapshot.id,
                action.id,
                ReconciliationDecision(
                    request_id="confirmed",
                    expected_revision=unknown.agent.snapshot.revision,
                    confirmed_status="succeeded",
                    result={"value": 1},
                    evidence_reference="external-record",
                    actor="operator",
                ),
            )
            runtime_invariants(two, run)
            assert await first.tick()
            delayed = runtime_invariants(one, run)[key]
            assert (
                delayed.agent.snapshot.status == "ready_model"
                and delayed.step["error"] == "ModelRetryable"
            )
            assert delayed.step["attempts"] == 1 and provider.calls == 2
            assert not await second.tick()
            another = one.submit(
                one.register(
                    WorkflowSpec(
                        name="another", steps=(StepSpec(id="free", handler="plain"),)
                    )
                ),
                {},
            )
            assert await second.tick()
            assert two.get_run(another)["status"] == "succeeded"
            with one.transaction() as db:
                db.execute(
                    update(StepRow)
                    .where(StepRow.run_id == run, StepRow.key == key)
                    .values(available_at=one._now(db) - 1)
                )
            if finish == "cancel_due_retry":
                barrier = Barrier(2)

                def race(index):
                    barrier.wait(timeout=3)
                    if index == 0:
                        return one.claim(300)
                    two.cancel(run)

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(race, index) for index in range(2)]
                    claimed = [f.result(timeout=10) for f in futures]
                if claimed[0] is not None:
                    runtime_invariants.fenced(agents, claimed[0])
                assert not await first.tick()
                assert provider.calls == 2
            else:
                before = len(tasks)
                await asyncio.wait_for(asyncio.gather(first.tick(), second.tick()), 10)
                for _ in range(3):
                    await first.tick()
                    if one.get_run(run)["status"] == "succeeded":
                        break
                final = runtime_invariants(two, run)[key]
                assert len(tasks) == before + 1  # 同一控制 Step 只被一个 Worker 领取。
                assert final.agent.snapshot.budget.model_rounds == 2
                assert final.agent.snapshot.budget.model_attempts == 3
                assert final.agent.snapshot.budget.tool_calls == 1
                assert final.step["attempts"] == 1 and len(invocations) == 1
                assert one.get_run(run)["status"] == "succeeded"
                assert plain_calls.count((run, "after")) == 1
                assert len(one.artifacts(run)) == 3
                assert (
                    len([m for m in provider.contexts[-1] if m["role"] == "tool"]) == 1
                )
        checked = runtime_invariants(two, run)[key]
        if finish != "complete":
            assert checked.run_status == "cancelled"
            assert not any(
                s["id"] == "after" and s["status"] == "succeeded"
                for s in one.get_run(run)["steps"]
            )
        assert two.get_run(independent) == isolated
        for task in tasks:
            runtime_invariants.fenced(agents, task)
        registry = one.metrics.registry
        assert (
            registry.get_sample_value(
                "agentflow_agent_reviews_total",
                {"kind": "approval", "outcome": "requested"},
            )
            == 1
        )
        assert (
            registry.get_sample_value(
                "agentflow_agent_reviews_total",
                {"kind": "approval", "outcome": "approved"},
            )
            == 1
        )
        if finish != "cancel_after_approval":
            assert (
                registry.get_sample_value(
                    "agentflow_agent_reviews_total",
                    {"kind": "reconciliation", "outcome": "succeeded"},
                )
                == 1
            )
            assert registry.get_sample_value("agentflow_agent_model_retries_total") == 1
            assert (
                registry.get_sample_value("agentflow_agent_action_reservations_total")
                == 1
            )


@pytest.mark.asyncio
async def test_shutdown_preserves_waiting_delayed_and_running(
    workers, runtime_invariants
):
    async with asyncio.timeout(20):
        one, two = workers
        entered = asyncio.Event()
        providers = {}
        handlers = HandlerRegistry()
        runtime = ToolRuntime()

        async def forbidden(args):
            pytest.fail("unapproved tool must not run")

        runtime.register(
            Tool(
                "write", "write", {"type": "object"}, forbidden, requires_approval=True
            )
        )

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        async def handler(task):
            return await DurableAgentRunner(
                SQLAgentStore(one), providers[task.run_id], runtime
            ).run(task, "goal", allowed={"write"})

        handlers.register("agent", handler)
        runs = []
        for stage in ("waiting", "delayed", "running"):
            run = one.submit(
                one.register(
                    WorkflowSpec(
                        name=stage,
                        steps=(
                            StepSpec(
                                id="work",
                                handler="agent",
                                parameters={"agent": {"retry_base_seconds": 60}},
                            ),
                        ),
                    )
                ),
                {},
            )
            runs.append(run)
            providers[run] = MockProvider(
                [tool_proposal()] if stage == "waiting" else [ModelRetryableError()]
            )
            if stage == "running":
                providers[run].respond = blocked
            else:
                assert await Worker(one, handlers).tick()
        waiting = runtime_invariants(one, runs[0])
        delayed = runtime_invariants(one, runs[1])
        serving = asyncio.create_task(Worker(one, handlers).serve(asyncio.Event()))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            runtime_invariants(two, runs[2])
            serving.cancel("shutdown")
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(serving, 6)
            assert runtime_invariants(two, runs[0]) == waiting
            assert runtime_invariants(two, runs[1]) == delayed
            interrupted = runtime_invariants(two, runs[2])["work"]
            assert interrupted.agent.snapshot.status == "failed"
            assert interrupted.agent.model_calls[-1].status == "unknown"
            assert not await Worker(two, handlers).tick()
        finally:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
