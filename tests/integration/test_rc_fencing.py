"""Minimal private lease records still exercise real claimed-token fencing."""

import json
from pathlib import Path

import pytest
from test_durable_agent_runner import store as store

from agentflow.domain.agent.budget import AgentBudget
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec


def test_minimal_soak_records_preserve_old_token_fencing(store, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from rc_workload import Workload, private_records, record, task_fence_record

    workload = Workload(
        store.engine.url.render_as_string(hide_password=False), tmp_path / "records"
    )
    try:
        run = store.submit(
            store.register(
                WorkflowSpec(
                    name="private-fencing",
                    steps=(
                        StepSpec(id="before", handler="plain"),
                        StepSpec(
                            id="agent",
                            handler="rc-agent",
                            depends_on=("before",),
                            parameters={"prompt": "PRIVATE_PROMPT"},
                        ),
                    ),
                )
            ),
            {"document": "PRIVATE_INPUT"},
        )
        upstream = store.claim(30)
        store.complete(upstream, {"document": "PRIVATE_DEPENDENCY"})
        task = store.claim(30)
        assert task.inputs and task.dependencies
        workload.agents.open(
            task,
            AgentSnapshot(
                id="rc-fence-session",
                budget=AgentBudget(
                    max_model_rounds=1,
                    max_model_attempts=1,
                    max_tool_calls=0,
                    max_resource_units=0,
                ),
            ),
        )
        root = private_records(workload.root)
        record(root, "claim", task=task_fence_record(task))
        store.cancel(run)
        seen = []
        load = workload.agents.load

        def observe(restored):
            seen.append(restored)
            return load(restored)

        monkeypatch.setattr(workload.agents, "load", observe)
        workload.fenced_claims()  # Real LostLease for load AND commit_transition.
        assert len(seen) == 1 and seen[0].token == task.token
        assert seen[0].spec == task.spec
        assert seen[0].inputs == {} and seen[0].dependencies == {}
        if "PRIVATE" in json.dumps(workload.events()):
            pytest.fail("SoakDiagnosticContainsPrivateTaskData", pytrace=False)
    finally:
        workload.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["postgres"], indirect=True)
async def test_backup_stops_writers_under_run_lock_even_when_deferral_becomes_due(
    store, monkeypatch, advance_database_clock
):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from test_provider_admission import opened, policy, reservation

    from agentflow.application.agents.durable_runner import DurableAgentRunner
    from agentflow.application.tools.runtime import ToolRuntime
    from agentflow.infrastructure.models.mock import MockProvider
    from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
    from agentflow.infrastructure.persistence.store import Store

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from verify_runtime_backup import stop_at_admission_deferral

    agents = SQLAgentStore(store, admission_policy=policy())
    task, state = opened(agents)
    agents.reserve_model_attempt(task, reservation(state))
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="backup-barrier", steps=(StepSpec(id="agent", handler="agent"),)
            )
        ),
        {},
    )
    task = store.claim(300)
    assert task.run_id == run
    result = await DurableAgentRunner(
        agents, MockProvider(), ToolRuntime(), admission=agents
    ).run(task, "fixed goal", allowed=set())
    assert result.status == "admission_deferred"
    stopped = []

    def stop():
        advance_database_clock(10)
        other = Store(store.engine.url.render_as_string(hide_password=False))
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(other.claim, 300).result(timeout=5) is None
            stopped.append(True)
        finally:
            other.close()

    assert stop_at_admission_deferral(SimpleNamespace(store=store, stop=stop), run)
    assert stopped == [True]
    # Eligibility really advanced: after the guard unlocks, this Step can claim.
    assert store.claim(300).run_id == run
