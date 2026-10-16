import asyncio
import os
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import LostLease, StateConflict
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.review import ApprovalDecision, ReconciliationDecision
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import AgentSessionRow, Base, StepRow
from agentflow.infrastructure.persistence.store import Store


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    url = (
        os.environ.get("AGENTFLOW_TEST_POSTGRES_URL")
        if request.param == "postgres"
        else f"sqlite:///{(tmp_path / 'review-runner.db').as_posix()}"
    )
    if not url:
        pytest.skip("requires a dedicated PostgreSQL test database")
    store = Store(url)
    Base.metadata.drop_all(store.engine)
    store.create_schema()
    yield store
    Base.metadata.drop_all(store.engine)
    store.close()


def proposal(count=1):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"provider-{i}",
                "type": "function",
                "function": {"name": "write", "arguments": '{"value":1}'},
            }
            for i in range(count)
        ],
    }


def setup(store, *, protected=True, unknown=False, count=1, max_attempts=3):
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="review-runner",
                steps=(
                    StepSpec(
                        id="work",
                        handler="agent",
                        artifact=True,
                        retry_delay=0,
                        max_attempts=max_attempts,
                    ),
                ),
            )
        ),
        {},
    )
    invoked = []

    async def tool(args, invocation):
        invoked.append(invocation)
        if unknown:
            raise TimeoutError
        return {"value": args["value"]}

    runtime = ToolRuntime()
    definition = Tool(
        "write",
        "write",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
        lambda args: None,
        requires_approval=protected,
        side_effect=ToolSideEffect.IDEMPOTENT,
        invoke_durable=tool,
    )
    runtime.register(definition)
    provider = MockProvider([proposal(count), {"role": "assistant", "content": "done"}])
    agents = SQLAgentStore(store)
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(agents, provider, runtime).run(
            task, "goal", allowed={"write"}
        )

    handlers.register("agent", handler)
    return run, agents, runtime, provider, invoked, handlers, definition


@pytest.mark.asyncio
async def test_runner_approves_and_executes_original_actions_in_sequence(store):
    run, agents, runtime, provider, invoked, handlers, definition = setup(
        store, count=2
    )
    worker = Worker(store, handlers)
    assert await worker.tick()
    assert not invoked and provider.calls == 1
    first = agents.list_pending_action_approvals()[0]
    for index in range(2):
        pending = agents.list_pending_action_approvals()[0]
        history = agents.review_history(pending.session_id)
        agents.decide_action(
            pending.approval_id,
            ApprovalDecision(
                expected_revision=history["revision"],
                decision="approved",
                actor="operator",
            ),
        )
        assert await worker.tick()
        assert invoked[index].action_id == pending.action_id
    assert store.get_run(run)["status"] == "succeeded"
    assert len(store.artifacts(run)) == 1
    assert provider.calls == 2
    assert len(agents.review_history(first.session_id)["approvals"]) == 2


@pytest.mark.asyncio
async def test_consumed_approval_survives_cancel_and_unknown_is_not_reexecuted(store):
    run, agents, runtime, provider, invoked, handlers, definition = setup(store)
    started = asyncio.Event()

    async def blocked(args, invocation):
        invoked.append(invocation)
        started.set()
        await asyncio.Event().wait()

    runtime._tools["write"] = replace(definition, invoke_durable=blocked)
    worker = Worker(store, handlers)
    assert await worker.tick()
    approval = agents.list_pending_action_approvals()[0]
    agents.decide_action(
        approval.approval_id,
        ApprovalDecision(
            expected_revision=agents.review_history(approval.session_id)["revision"],
            decision="approved",
            actor="operator",
        ),
    )
    job = asyncio.create_task(worker.tick())
    await asyncio.wait_for(started.wait(), 5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert agents.get_action_approval(approval.approval_id).consumed_at is not None
    assert agents.list_unknown_actions()[0]["action_id"] == approval.action_id
    assert store.get_run(run)["status"] == "waiting"
    assert not await worker.tick()
    assert len(invoked) == 1 and provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "schema", "policy", "arguments"])
async def test_changed_tool_or_parameters_cannot_use_old_approval(store, change):
    from dataclasses import replace

    from sqlalchemy import update

    from agentflow.infrastructure.persistence.models import AgentActionRow

    run, agents, runtime, provider, invoked, handlers, definition = setup(store)
    worker = Worker(store, handlers)
    await worker.tick()
    pending = agents.list_pending_action_approvals()[0]
    revision = agents.review_history(pending.session_id)["revision"]
    agents.decide_action(
        pending.approval_id,
        ApprovalDecision(
            expected_revision=revision, decision="approved", actor="operator"
        ),
    )
    if change == "arguments":
        with store.transaction() as db:
            db.execute(update(AgentActionRow).values(arguments={"value": 2}))
    else:
        if change == "version":
            definition = replace(definition, version="v2")
        elif change == "schema":
            definition.schema["required"] = ["extra"]
        else:
            definition = replace(definition, timeout=1)
        runtime._tools["write"] = definition  # 模拟 Worker 部署了新的工具注册配置。
    await worker.tick()
    assert not invoked
    assert (
        not store.artifacts(run)
        if change == "arguments"
        else store.get_run(run)["status"] == "succeeded"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_unknown_reconciliation_resumes_without_reexecuting_tool(store, status):
    run, agents, runtime, provider, invoked, handlers, definition = setup(
        store, protected=False, unknown=True
    )
    worker = Worker(store, handlers)
    await worker.tick()
    pending = agents.list_unknown_actions()[0]
    agents.reconcile_action(
        pending["session_id"],
        pending["action_id"],
        ReconciliationDecision(
            request_id="evidence",
            expected_revision=pending["revision"],
            confirmed_status=status,
            error_code="ConfirmedFailed" if status == "failed" else None,
            result={"value": 1},
            evidence_reference="external:receipt",
            actor="operator",
        ),
    )
    await worker.tick()
    assert len(invoked) == 1
    assert store.get_run(run)["status"] == "succeeded"
    tools = [m for m in provider.contexts[-1] if m["role"] == "tool"]
    assert len(tools) == 1 and f'"status":"{status}"' in tools[0]["content"]


def approve_pending(agents):
    pending = agents.list_pending_action_approvals()[0]
    agents.decide_action(
        pending.approval_id,
        ApprovalDecision(
            expected_revision=agents.review_history(pending.session_id)["revision"],
            decision="approved",
            actor="operator",
        ),
    )
    return pending


@pytest.mark.asyncio
async def test_single_attempt_approval_is_a_continuation_not_a_retry(store):
    run, agents, runtime, provider, invoked, handlers, definition = setup(
        store, max_attempts=1
    )
    worker = Worker(store, handlers)
    assert await worker.tick()
    approve_pending(agents)
    store.engine.dispose()  # The one-shot continuation survives a reconnect.
    assert await worker.tick()
    assert store.get_run(run)["status"] == "succeeded"
    assert store.get_run(run)["steps"][0]["attempts"] == 1
    assert len(invoked) == 1 and provider.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("max_attempts", [1, 3])
async def test_two_approvals_then_crash_preserves_reconciliation_and_retry_bound(
    store, monkeypatch, max_attempts
):
    run, agents, runtime, provider, invoked, handlers, definition = setup(
        store, count=2, max_attempts=max_attempts
    )
    worker = Worker(store, handlers)
    assert await worker.tick()
    first = approve_pending(agents)
    assert await worker.tick()
    second = approve_pending(agents)
    saved = []
    commit = agents.commit_transition

    def record(task, transition):
        state = commit(task, transition)
        saved.append((task, state))
        return state

    class ProcessCrash(BaseException):
        pass

    async def crash(args, invocation):
        invoked.append(invocation)
        raise ProcessCrash

    monkeypatch.setattr(agents, "commit_transition", record)
    runtime._tools["write"] = replace(definition, invoke_durable=crash)
    with pytest.raises(ProcessCrash):
        await worker.tick()
    old_task, inflight = saved[-1]
    with store.transaction() as db:
        db.execute(update(StepRow).values(lease_until=0))
    assert store.claim(300) is None
    pending = agents.list_unknown_actions()[0]
    assert pending["agent_status"] == "waiting_reconciliation"
    assert store.get_run(run)["steps"][0]["attempts"] == 1
    with pytest.raises(LostLease):
        agents.load(old_task)
    agents.reconcile_action(
        pending["session_id"],
        pending["action_id"],
        ReconciliationDecision(
            request_id="crash-evidence",
            expected_revision=pending["revision"],
            confirmed_status="succeeded",
            result={"value": 1},
            evidence_reference="external:receipt",
            actor="operator",
        ),
    )
    runtime._tools["write"] = definition
    assert await worker.tick() is (max_attempts > 1)
    assert [i.action_id for i in invoked] == [first.action_id, second.action_id]
    assert len({i.idempotency_key for i in invoked}) == 2
    assert store.get_run(run)["status"] == (
        "succeeded" if max_attempts > 1 else "failed"
    )
    with store.transaction() as db:
        snapshot = db.scalar(select(AgentSessionRow)).snapshot
    assert snapshot["budget"]["tool_calls"] == 2
    assert snapshot["budget"]["model_rounds"] == (2 if max_attempts > 1 else 1)
    assert store.get_run(run)["steps"][0]["attempts"] == (2 if max_attempts > 1 else 1)


@pytest.mark.asyncio
async def test_successful_terminal_action_cannot_be_disguised_as_unknown_reconciliation(
    store,
):
    run, agents, runtime, provider, invoked, handlers, definition = setup(store)
    worker = Worker(store, handlers)
    await worker.tick()
    pending = approve_pending(agents)
    await worker.tick()
    before = store.get_run(run)
    with pytest.raises(StateConflict):
        agents.reconcile_action(
            pending.session_id,
            pending.action_id,
            ReconciliationDecision(
                request_id="contradictory-evidence",
                expected_revision=agents.review_history(pending.session_id)["revision"],
                confirmed_status="failed",
                error_code="ExternalFailure",
                evidence_reference="external:receipt",
                actor="operator",
            ),
        )
    assert not agents.review_history(pending.session_id)["reconciliations"]
    assert store.get_run(run) == before and not await worker.tick()
