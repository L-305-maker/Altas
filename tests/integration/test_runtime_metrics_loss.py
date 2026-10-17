"""Metrics loss is observable, but cannot change the database source of truth."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, generate_latest, multiprocess
from pydantic import SecretStr
from test_durable_agent_runner import store as store

from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics

PROGRAM = """
import asyncio, os, time
from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.workflow.spec import WorkflowSpec, StepSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics
store = Store(os.environ['RC_METRICS_DB'])
try:
    store.metrics = RuntimeMetrics()
except Exception:
    pass
run = store.submit(store.register(WorkflowSpec(name='metric-loss', steps=(StepSpec(id='work', handler='agent', artifact=True),))), {})
handlers = HandlerRegistry()
async def handler(task):
    return await DurableAgentRunner(SQLAgentStore(store), MockProvider(), ToolRuntime()).run(task, 'fixed public fixture', allowed=set())
handlers.register('agent', handler)
original = store._observe
def observe(method, *args):
    if os.environ.get('RC_CRASH_AFTER_COMMIT') == '1' and method == 'record' and args[0] == 'agent.transition' and args[1]['state'] == 'succeeded':
        os._exit(19)
    return original(method, *args)
store._observe = observe
async def finish():
    worker = Worker(store, handlers)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        await worker.tick()
        runs = store.list_runs()
        if len(runs) == int(os.environ['RC_EXPECTED_RUNS']) and all(r['status'] == 'succeeded' for r in runs):
            return
        await asyncio.sleep(0.01)
    raise AssertionError('MetricWorkerProgressTimeout')
asyncio.run(finish())
store.close()
"""


@pytest.mark.asyncio
async def test_failed_collector_endpoint_does_not_prevent_agent_completion(
    store, monkeypatch
):
    from agentflow.application.orchestration.worker import Worker
    from agentflow.bootstrap import build_handlers
    from agentflow.config import Settings
    from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
    from agentflow.interfaces.api.app import create_app

    settings = Settings(
        provider="mock", telemetry=True, api_token=SecretStr("test-operator-token")
    )
    app = create_app(settings, store)

    def broken():
        raise RuntimeError("SyntheticCollectorFailure")

    monkeypatch.setattr(store.metrics.registry, "collect", broken)
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="collector-down",
                steps=(
                    StepSpec(
                        id="agent",
                        handler="agent",
                        artifact=True,
                        parameters={
                            "prompt": "fixed public fixture",
                            "agent": {"mode": "durable_v1"},
                        },
                    ),
                ),
            )
        ),
        {},
    )
    with TestClient(app) as client:
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer test-operator-token"}
            ).status_code
            == 503
        )
        assert await Worker(store, build_handlers(settings, store=store)).tick()
        assert client.get("/health").status_code == 200
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer test-operator-token"}
            ).status_code
            == 503
        )
    assert (
        store.get_run(run)["status"] == "succeeded" and len(store.artifacts(run)) == 1
    )


def child(store, environment):
    return subprocess.Popen(
        [sys.executable, "-c", PROGRAM],
        env=dict(
            os.environ,
            RC_METRICS_DB=store.engine.url.render_as_string(hide_password=False),
            **environment,
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@pytest.mark.parametrize(
    "mode", ["commit_crash", "shared_workers", "unwritable_directory"]
)
def test_metric_process_failure_does_not_change_persistent_facts(
    store, tmp_path, runtime_invariants, mode
):
    directory = tmp_path / "metrics"
    directory.mkdir()
    environment = {
        "PROMETHEUS_MULTIPROC_DIR": str(directory),
        "RC_EXPECTED_RUNS": "3" if mode == "shared_workers" else "1",
    }
    if mode == "commit_crash":
        environment["RC_CRASH_AFTER_COMMIT"] = "1"
    if mode == "unwritable_directory":
        # File-as-directory fails with ENOTDIR/PermissionError on every OS,
        # including privileged CI users for whom chmod(0) is still writable.
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("blocked")
        environment["PROMETHEUS_MULTIPROC_DIR"] = str(blocked)
    children = [
        child(store, environment) for _ in range(3 if mode == "shared_workers" else 1)
    ]
    try:
        for process in children:
            assert process.wait(timeout=30) == (19 if mode == "commit_crash" else 0)
    finally:
        for process in children:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    process.kill()
                process.wait(timeout=10)
    runs = store.list_runs()
    assert len(runs) == len(children)
    for run in runs:
        assert run["status"] == "succeeded"
        checkpoint = runtime_invariants(store, run["id"])["work"]
        assert len(checkpoint.artifacts) == 1
        assert checkpoint.agent.snapshot.budget.model_attempts == 1
    # Fresh process-local registry resets even though durable facts survive.
    fresh = RuntimeMetrics()
    assert (
        fresh.registry.get_sample_value(
            "agentflow_agent_model_requests_total", {"outcome": "reserved"}
        )
        is None
    )
    if mode != "unwritable_directory":
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry, path=str(directory))
        output = generate_latest(registry).decode()
        assert "fixed public fixture" not in output
        if mode == "commit_crash":
            assert 'agentflow_agent_transitions_total{state="succeeded"}' not in output
        else:
            assert (
                'agentflow_agent_model_requests_total{outcome="reserved"} 3.0' in output
            )
            api = """
import os
from fastapi.testclient import TestClient
from pydantic import SecretStr
from agentflow.config import Settings
from agentflow.infrastructure.persistence.store import Store
from agentflow.interfaces.api.app import create_app
store = Store(os.environ['RC_METRICS_DB'])
with TestClient(create_app(Settings(telemetry=True, api_token=SecretStr('test-operator-token')), store)) as client:
    assert client.get('/metrics').status_code == 401
    response = client.get('/metrics', headers={'Authorization': 'Bearer test-operator-token'})
    assert response.status_code == 200
    assert 'agentflow_agent_model_requests_total{outcome="reserved"} 3.0' in response.text
store.close()
"""
            result = subprocess.run(
                [sys.executable, "-c", api],
                env=dict(
                    os.environ,
                    RC_METRICS_DB=store.engine.url.render_as_string(
                        hide_password=False
                    ),
                    **environment,
                ),
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert result.returncode == 0, "AuthenticatedMultiprocessScrapeFailed"
    restart = """
import os
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics
assert RuntimeMetrics().registry.get_sample_value('agentflow_agent_model_requests_total', {'outcome':'reserved'}) is None
store = Store(os.environ['RC_METRICS_DB'])
assert all(r['status'] == 'succeeded' for r in store.list_runs())
store.close()
"""
    environment = dict(
        os.environ, RC_METRICS_DB=store.engine.url.render_as_string(hide_password=False)
    )
    environment.pop("PROMETHEUS_MULTIPROC_DIR", None)
    result = subprocess.run(
        [sys.executable, "-c", restart],
        env=environment,
        capture_output=True,
        timeout=20,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, "LocalCounterRestartFailed"
