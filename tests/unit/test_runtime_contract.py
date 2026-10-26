"""Capability boundaries, not an exactly-once external-effects claim."""

import importlib

import pytest
from sqlalchemy import func, select

from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import AgentSessionRow
from agentflow.infrastructure.persistence.store import ConflictError, Store


@pytest.fixture
def store(tmp_path):
    value = Store(f"sqlite:///{(tmp_path / 'contract.db').as_posix()}")
    value.create_schema()
    try:
        yield value
    finally:
        value.close()


def submit(store, *, parameters=None, handler="agent", version=1):
    spec = WorkflowSpec(
        name="runtime-contract",
        version=version,
        steps=(
            StepSpec(
                id="work",
                handler=handler,
                artifact=True,
                retry_delay=0,
                parameters=parameters or {},
            ),
        ),
    )
    return store.submit(store.register(spec), {}), spec


def sessions(store):
    with store.transaction() as db:
        return db.scalar(select(func.count()).select_from(AgentSessionRow))


def test_public_facade_reuses_existing_types():
    public = importlib.import_module("agentflow.runtime")
    assert public.WorkflowSpec is WorkflowSpec
    assert public.StepSpec is StepSpec
    assert public.HandlerRegistry is HandlerRegistry
    assert public.Worker is Worker and public.Store is Store
    assert {
        "AgentRunner",
        "DurableAgentRunner",
        "ExecutionStore",
        "AgentStore",
        "ModelProvider",
        "DurableModelProvider",
        "RequestModelProvider",
        "ToolRuntime",
        "CommittedAgentResult",
        "create_app",
    } <= set(public.__all__)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", (None, "durable_v1"))
async def test_only_explicit_durable_mode_reserves_and_commits(
    store, monkeypatch, mode
):
    import agentflow.bootstrap as bootstrap

    wrong = "DurableAgentRunner" if mode is None else "AgentRunner"
    monkeypatch.setattr(bootstrap, wrong, lambda *a, **k: pytest.fail("WrongAgentMode"))
    run, _ = submit(store, parameters={} if mode is None else {"agent": {"mode": mode}})
    completed = []
    actual = store.complete

    def complete(*args):
        completed.append(True)
        return actual(*args)

    monkeypatch.setattr(store, "complete", complete)
    assert await Worker(
        store, build_handlers(Settings(provider="mock"), store=store)
    ).tick()
    assert store.get_run(run)["status"] == "succeeded"
    assert sessions(store) == int(mode == "durable_v1")
    assert len(completed) == int(mode is None)
    assert len(store.artifacts(run)) == 1


@pytest.mark.asyncio
async def test_plain_step_repeats_effect_after_lost_completion(
    store, advance_database_clock
):
    run, _ = submit(store, handler="plain")
    effects = []

    async def plain(task):
        effects.append(task.idempotency_key)
        return {"value": 1}

    handlers = HandlerRegistry()
    handlers.register("plain", plain)
    old = store.claim(1)
    await plain(old)  # Effect happened; process disappeared before Store.complete.
    advance_database_clock(2)
    assert await Worker(store, handlers).tick()
    assert effects == [old.idempotency_key, old.idempotency_key]
    assert store.get_run(run)["steps"][0]["attempts"] == 2
    assert len(store.artifacts(run)) == 1
    assert store.complete(old, {"late": True}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters,with_store",
    (
        ({"agent": {"mode": "unknown"}}, True),
        ({"agent": {"mode": "durable_v1"}}, False),
    ),
)
async def test_invalid_or_unavailable_mode_never_falls_back(
    store, monkeypatch, parameters, with_store
):
    import agentflow.bootstrap as bootstrap

    monkeypatch.setattr(
        bootstrap, "AgentRunner", lambda *a, **k: pytest.fail("LegacyFallback")
    )
    run, _ = submit(store, parameters=parameters)
    registry = build_handlers(
        Settings(provider="mock"), store=store if with_store else None
    )
    assert await Worker(store, registry).tick()
    assert store.get_run(run)["steps"][0]["error"] == "ValueError"
    assert sessions(store) == 0


def test_registration_and_submission_snapshot_do_not_follow_new_defaults(store):
    run, original = submit(store)
    changed = original.model_copy(
        update={
            "steps": (
                StepSpec(
                    id="work",
                    handler="agent",
                    parameters={"agent": {"mode": "durable_v1"}},
                ),
            )
        }
    )
    with pytest.raises(ConflictError):
        store.register(changed)
    store.register(changed.model_copy(update={"version": 2}))
    registry = build_handlers(Settings(provider="mock"), store=store)

    async def added(_task):
        return {"other": True}

    registry.register("added.v2", added)
    with pytest.raises(ValueError):
        registry.register("agent", added)
    assert store.get_run(run)["spec"] == original.model_dump(mode="json")
    task = store.claim()
    assert task.spec.parameters == {} and task.spec.artifact
    assert "mode" not in task.spec.parameters.get("agent", {})


@pytest.mark.asyncio
async def test_respond_alone_is_not_the_current_durable_request_contract(store):
    from agentflow.application.agents.durable_runner import DurableAgentRunner
    from agentflow.application.tools.runtime import ToolRuntime
    from agentflow.infrastructure.persistence.agent_store import SQLAgentStore

    class RespondOnly:
        async def respond(self, *args):
            pytest.fail("UnreservedDispatch")

    run, _ = submit(store, parameters={"agent": {"mode": "durable_v1"}})
    task = store.claim()
    result = await DurableAgentRunner(
        SQLAgentStore(store), RespondOnly(), ToolRuntime()
    ).run(task, "synthetic", allowed=set())
    store.validate_agent_result(task, result)
    assert result.status == "failed" and store.get_run(run)["status"] == "failed"
    assert store.get_run(run)["steps"][0]["error"] == "ModelRequestContractUnavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", (False, True))
async def test_unknown_model_outcome_is_only_durably_reserved_in_explicit_mode(
    store, monkeypatch, durable
):
    import agentflow.bootstrap as bootstrap
    from agentflow.runtime import MockProvider, ModelOutcomeUnknown

    provider = MockProvider(
        [ModelOutcomeUnknown(), {"role": "assistant", "content": "second call"}]
    )
    monkeypatch.setattr(bootstrap, "build_provider", lambda settings: provider)
    run, _ = submit(
        store, parameters={"agent": {"mode": "durable_v1"}} if durable else {}
    )
    worker = Worker(store, build_handlers(Settings(provider="mock"), store=store))
    assert await worker.tick()
    retried = await worker.tick()
    assert retried is (not durable)
    assert provider.calls == (1 if durable else 2)
    assert store.get_run(run)["status"] == ("failed" if durable else "succeeded")
    assert sessions(store) == int(durable)


@pytest.mark.asyncio
async def test_a_committed_result_claim_is_not_a_commit_or_external_effect_proof(store):
    from agentflow.runtime import CommittedAgentResult

    run, _ = submit(store, handler="receipt")

    async def forged(task):
        return CommittedAgentResult(
            task.run_id, task.step_id, task.token, "succeeded", 1
        )

    handlers = HandlerRegistry()
    handlers.register("receipt", forged)
    assert await Worker(store, handlers).tick()
    assert store.get_run(run)["status"] == "running"
    assert not store.artifacts(run) and sessions(store) == 0
