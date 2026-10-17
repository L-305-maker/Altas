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
