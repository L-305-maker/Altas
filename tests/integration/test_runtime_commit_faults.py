import asyncio
from contextlib import contextmanager

import pytest
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import final, proposal, submit, tools
from test_durable_agent_runner import store as store

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import AgentStoreUnavailable
from agentflow.application.ports.model_provider import (
    ModelResponse,
    ModelRetryableError,
)
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import StepRow


@pytest.mark.asyncio
@pytest.mark.parametrize("profiled", [False, True])
@pytest.mark.parametrize(
    "point",
    [
        "response_rollback",
        "response_lost_ack",
        "tool_lost_ack",
        "terminal_rollback",
        "terminal_lost_ack",
        "retry_lost_ack",
    ],
)
async def test_worker_commit_fault_recovers_persisted_facts(
    store, runtime_invariants, monkeypatch, point, profiled
):
    async with asyncio.timeout(15):
        run = submit(store, retry_base_seconds=60)
        agents = SQLAgentStore(store)
        measured = ModelResponse(
            assistant_message=final(),
            finish_reason="stop",
            usage={"total_tokens": 13},
        )
        provider = MockProvider(
            [ModelRetryableError(), measured]
            if point == "retry_lost_ack"
            else [proposal(), measured]
        )
        if profiled:
            from token_counter_fixtures import Counter, capabilities

            from agentflow.application.ports.model_request import ModelRequestOptions

            provider.token_counter = Counter()
            provider.capabilities = capabilities()
            provider.request_options = ModelRequestOptions(
                max_output_tokens=10, strict_token_window=True
            )
        invocations, tasks = [], []

        async def invoke(args):
            invocations.append(args)
            return {"value": 1}

        runtime = tools(invoke)
        method = (
            "complete_agent_step"
            if point.startswith("terminal")
            else "defer_model_retry"
            if point == "retry_lost_ack"
            else "commit_transition"
        )
        original, armed = getattr(agents, method), True

        def fail_commit(*args):
            raise OperationalError("injected connection loss", None, None)

        def fault(task, transition, *args):
            nonlocal armed
            matches = method != "commit_transition" or (
                transition.snapshot.status == "actions_pending"
                and (
                    bool(transition.observations)
                    if point == "tool_lost_ack"
                    else bool(transition.messages)
                )
            )
            if not armed or not matches:
                return original(task, transition, *args)
            armed = False
            if point.endswith("rollback"):
                event.listen(store.engine, "commit", fail_commit)
                try:
                    return original(task, transition, *args)
                finally:
                    event.remove(store.engine, "commit", fail_commit)
            original(task, transition, *args)
            raise AgentStoreUnavailable("commit acknowledgement lost")

        monkeypatch.setattr(agents, method, fault)
        handlers = HandlerRegistry()

        async def handler(task):
            tasks.append(task)
            return await DurableAgentRunner(agents, provider, runtime).run(
                task, "goal", allowed={"echo"}
            )

        handlers.register("agent", handler)
        worker = Worker(store, handlers, lease_seconds=300)
        assert await worker.tick()
        before = runtime_invariants(store, run)["work"]
        assert not armed
        if point == "terminal_lost_ack":
            assert (
                before.agent.snapshot.status == "succeeded"
                and len(before.artifacts) == 1
            )
            assert not await worker.tick()
        else:
            with store.transaction() as db:
                values = (
                    {"available_at": store._now(db) - 1}
                    if point == "retry_lost_ack"
                    else {"lease_until": store._now(db) - 1}
                )
                db.execute(
                    update(StepRow).where(StepRow.run_id == run).values(**values)
                )
            assert await worker.tick()
        after = runtime_invariants(store, run)["work"]
        if point.endswith("rollback"):
            assert before.agent.snapshot.status == "model_inflight"
            assert after.agent.snapshot.status == "failed"
            assert after.agent.model_calls[-1].status == "unknown"
            assert after.agent.model_attempts[-1].outcome == "unknown"
            assert after.agent.model_attempts[-1].raw_usage is None
            assert after.agent.usage_summary().total_tokens.value is None
            assert not after.artifacts  # 已收到的响应不能在未持久化后凭空复原。
            assert provider.calls == (1 if point.startswith("response") else 2)
        else:
            assert after.agent.snapshot.status == "succeeded"
            assert len(after.artifacts) == 1
            assert len(invocations) == int(point != "retry_lost_ack")
            assert after.agent.snapshot.budget.tool_calls == len(invocations)
            assert after.agent.usage_summary().total_tokens.value == 13
            assert after.agent.usage_summary().total_tokens.reported_attempts == 1
            assert len(after.agent.model_attempts) == 2
        runtime_invariants.fenced(agents, tasks[0])


@pytest.mark.parametrize("phase", ["approval", "reconciliation"])
@pytest.mark.parametrize("failure", ["rollback", "lost_ack"])
def test_operator_connection_fault_preserves_audit_and_idempotency(
    store, runtime_invariants, monkeypatch, phase, failure
):
    from test_action_reviews import batch, decision, reconcile, wait_unknown

    agents = SQLAgentStore(store)
    if phase == "approval":
        run, task, state = batch(agents)
        state = agents.request_action_approval(
            task, "action-0", state.snapshot.revision
        )
        request = decision(state)

        def operate():
            return agents.decide_action(state.approvals[0].approval_id, request)
    else:
        run, task, state = wait_unknown(agents)
        request = reconcile(state)

        def operate():
            return agents.reconcile_action(state.snapshot.id, "action-0", request)

    before = runtime_invariants(store, run)
    original = store.transaction

    def fail_commit(connection):
        if failure == "lost_ack" or connection.info.pop("fail_operator_commit", False):
            raise OperationalError("private connection failure", None, None)

    @contextmanager
    def lose_ack():
        with original() as db:
            yield db
            written = bool(db.info.get("operator_written"))
        if written:
            raise OperationalError("private connection failure", None, None)

    # Tag only the actual operator write, rather than preceding read transactions.
    old_event = store._event

    def mark(db, run_id, kind, data):
        if kind in {"agent.approval_decided", "agent.reconciled"}:
            db.info["operator_written"] = True
            if failure == "rollback":
                db.connection().info["fail_operator_commit"] = True
        old_event(db, run_id, kind, data)

    monkeypatch.setattr(store, "_event", mark)
    if failure == "rollback":
        event.listen(store.engine, "commit", fail_commit)
    else:
        monkeypatch.setattr(store, "transaction", lose_ack)
    try:
        with pytest.raises(AgentStoreUnavailable):
            operate()
    finally:
        event.remove(
            store.engine, "commit", fail_commit
        ) if failure == "rollback" else monkeypatch.setattr(
            store, "transaction", original
        )
    after = runtime_invariants(store, run)
    if failure == "rollback":
        assert after == before
    saved = operate()
    assert operate() == saved
    current = runtime_invariants(store, run)
    assert (
        len(
            current["work"].agent.approvals
            if phase == "approval"
            else current["work"].agent.reconciliations
        )
        == 1
    )
