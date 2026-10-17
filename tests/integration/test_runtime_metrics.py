import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from test_durable_agent_runner import store as store

from agentflow.application.agents.durable_runner import DurableAgentRunner, successor
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import AgentTransition, StateConflict
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.config import Settings
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import ArtifactRow
from agentflow.interfaces.api.app import create_app


def runtime_metrics():
    from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics

    return RuntimeMetrics()


def value(metrics, name, labels=None):
    return metrics.registry.get_sample_value(name, labels or {}) or 0


@pytest.mark.asyncio
async def test_metrics_only_count_confirmed_commits_and_fail_open(store, monkeypatch):
    metrics = runtime_metrics()
    store.metrics = metrics
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="metrics",
                steps=(StepSpec(id="work", handler="agent", artifact=True),),
            )
        ),
        {},
    )
    agents = SQLAgentStore(store)
    tasks = []
    handlers = HandlerRegistry()

    async def handler(task):
        tasks.append(task)
        return await DurableAgentRunner(agents, MockProvider(), ToolRuntime()).run(
            task, "PRIVATE_PROMPT", allowed=set()
        )

    handlers.register("agent", handler)

    def fail(*args):
        raise IntegrityError("private failure", None, None)

    event.listen(ArtifactRow, "before_insert", fail)
    try:
        await Worker(store, handlers, observe=store.observe_execution).tick()
    finally:
        event.remove(ArtifactRow, "before_insert", fail)
    assert (
        value(metrics, "agentflow_agent_transitions_total", {"state": "model_inflight"})
        == 1
    )
    assert (
        value(metrics, "agentflow_agent_transitions_total", {"state": "succeeded"}) == 0
    )
    assert (
        value(metrics, "agentflow_agent_model_requests_total", {"outcome": "succeeded"})
        == 0
    )
    assert not store.artifacts(run)
    assert (
        value(
            metrics, "agentflow_store_transaction_seconds_count", {"outcome": "error"}
        )
        >= 1
    )
    with pytest.raises(StateConflict):
        agents.commit_transition(
            tasks[0],
            AgentTransition(
                snapshot=successor(agents.load(tasks[0]).snapshot, revision=999)
            ),
        )
    assert (
        value(metrics, "agentflow_runtime_failures_total", {"kind": "state_conflict"})
        >= 1
    )
    assert "PRIVATE_PROMPT" not in generate_latest(metrics.registry).decode()
    assert store.get_run(run)["steps"][0]["status"] == "running"


def test_labels_are_finite_and_do_not_export_untrusted_fields():
    metrics = runtime_metrics()
    for index in range(30):
        private = f"PRIVATE_{index}"
        metrics.record("agent.transition", {"state": private, "run_id": private})
        metrics.record(
            "agent.approval_decided", {"decision": private, "actor": private}
        )
        metrics.record(
            "agent.reconciliation_outcome", {"status": private, "evidence": private}
        )
    metrics.record("agent.transition", {"state": "succeeded", "prompt": "PRIVATE"})
    metrics.record("agent.action_reserved", {"count": 2})
    metrics.record(
        "agent.approval_decided", {"decision": "approved", "actor": "PRIVATE"}
    )
    metrics.record(
        "agent.reconciliation_outcome", {"status": "failed", "applied": True}
    )
    metrics.execution("returned", 1e12)
    assert value(metrics, "agentflow_agent_action_reservations_total") == 2
    assert (
        value(
            metrics,
            "agentflow_agent_reviews_total",
            {"kind": "reconciliation", "outcome": "failed"},
        )
        == 1
    )
    assert (
        value(
            metrics, "agentflow_worker_execution_seconds_sum", {"outcome": "returned"}
        )
        <= 60
    )
    output = generate_latest(metrics.registry).decode()
    assert "PRIVATE" not in output
    for collector in metrics.registry.collect():
        for sample in collector.samples:
            assert set(sample.labels) <= {"state", "outcome", "kind", "le"}


@pytest.mark.asyncio
async def test_broken_metric_backend_never_breaks_runtime(store, monkeypatch):
    metrics = runtime_metrics()
    store.metrics = metrics

    def broken(*args):
        raise RuntimeError("metrics unavailable")

    monkeypatch.setattr(metrics, "record", broken)
    monkeypatch.setattr(metrics, "transaction", broken)
    monkeypatch.setattr(metrics, "execution", broken)
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="broken-metrics", steps=(StepSpec(id="work", handler="agent"),)
            )
        ),
        {},
    )
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            SQLAgentStore(store), MockProvider(), ToolRuntime()
        ).run(task, "goal", allowed=set())

    handlers.register("agent", handler)
    assert await Worker(store, handlers, observe=store.observe_execution).tick()
    assert store.get_run(run)["status"] == "succeeded"


def test_metrics_remain_opt_in_authenticated_and_http_fail_open(store, monkeypatch):
    settings = Settings(api_token=SecretStr("test-operator-token"), telemetry=False)
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/metrics").status_code == 404
    settings = Settings(api_token=SecretStr("test-operator-token"), telemetry=True)
    app = create_app(settings, store)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer test-operator-token"}
            ).status_code
            == 200
        )

        def unavailable(*args):
            raise RuntimeError("private metric failure")

        import agentflow.infrastructure.telemetry.metrics as module

        monkeypatch.setattr(module, "generate_latest", unavailable)
        monkeypatch.setattr(module.Counter, "labels", unavailable)
        assert client.get("/health").status_code == 200
        response = client.get(
            "/metrics", headers={"Authorization": "Bearer test-operator-token"}
        )
        assert response.status_code == 503 and "private" not in response.text


def test_worker_metrics_share_authenticated_scrape_without_new_server(tmp_path):
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    database = f"sqlite:///{(tmp_path / 'processes.db').as_posix()}"
    common = """
import os
from agentflow.infrastructure.persistence.store import Store
store = Store(os.environ['TEST_RUNTIME_DB'])
"""
    worker = """
import asyncio
from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.workflow.spec import WorkflowSpec, StepSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics
store.metrics = RuntimeMetrics()
store.create_schema()
run = store.submit(store.register(WorkflowSpec(name='multiprocess', steps=(StepSpec(id='work', handler='agent'),))), {})
handlers = HandlerRegistry()
async def handler(task):
    return await DurableAgentRunner(SQLAgentStore(store), MockProvider(), ToolRuntime()).run(task, 'PRIVATE_PROMPT', allowed=set())
handlers.register('agent', handler)
assert asyncio.run(Worker(store, handlers, observe=store.observe_execution).tick())
store.close()
"""
    api = """
from fastapi.testclient import TestClient
from pydantic import SecretStr
from agentflow.config import Settings
from agentflow.interfaces.api.app import create_app
with TestClient(create_app(Settings(telemetry=True, api_token=SecretStr('test-operator-token')), store)) as client:
    assert client.get('/metrics').status_code == 401
    text = client.get('/metrics', headers={'Authorization':'Bearer test-operator-token'}).text
    assert 'agentflow_agent_model_requests_total{outcome="reserved"} 1.0' in text
    assert 'PRIVATE' not in text
store.close()
"""
    disabled = (
        """
import builtins
original = builtins.__import__
def unavailable(name, *args, **kwargs):
    if name == 'agentflow.infrastructure.telemetry.metrics':
        raise ImportError('optional metrics module unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = unavailable
"""
        + common
        + """
from pydantic import SecretStr
from fastapi.testclient import TestClient
from agentflow.config import Settings
from agentflow.interfaces.api.app import create_app
assert store.list_runs()[0]['status'] == 'succeeded'
with TestClient(create_app(Settings(telemetry=False, api_token=SecretStr('test-operator-token')), store)) as client:
    assert client.get('/health').status_code == 200
store.close()
"""
    )
    environment = dict(
        os.environ, TEST_RUNTIME_DB=database, PROMETHEUS_MULTIPROC_DIR=str(metrics_dir)
    )
    for program in (common + worker, common + api, disabled):
        child = subprocess.run(
            [sys.executable, "-I", "-c", program],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert child.returncode == 0, child.stderr


def test_two_apps_can_share_store_metrics_without_duplicate_collectors(store):
    settings = Settings(telemetry=True, api_token=SecretStr("test-operator-token"))
    apps = [create_app(settings, store), create_app(settings, store)]
    store.metrics.record("agent.model", {"outcome": "reserved"})
    for app in apps:
        with TestClient(app) as client:
            response = client.get(
                "/metrics", headers={"Authorization": "Bearer test-operator-token"}
            )
            assert response.status_code == 200
            assert (
                'agentflow_agent_model_requests_total{outcome="reserved"} 1.0'
                in response.text
            )


@pytest.mark.asyncio
async def test_metric_initialization_failure_does_not_stop_api_or_worker(
    store, monkeypatch
):
    import agentflow.infrastructure.telemetry.metrics as module
    from agentflow.interfaces.cli.main import serve_worker

    def unavailable(*args):
        raise PermissionError("metric directory unavailable")

    monkeypatch.setattr(module, "RuntimeMetrics", unavailable)
    settings = Settings(
        telemetry=True,
        api_token=SecretStr("test-operator-token"),
        database_url=SecretStr(store.engine.url.render_as_string(hide_password=False)),
    )
    with TestClient(create_app(settings, store)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 401
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer test-operator-token"}
            ).status_code
            == 503
        )
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="metrics-startup", steps=(StepSpec(id="plain", handler="plain"),)
            )
        ),
        {},
    )
    handlers = HandlerRegistry()

    async def plain(task):
        return {"ok": True}

    handlers.register("plain", plain)
    await serve_worker(
        settings, once=True, handler_factory=lambda settings, tools: handlers
    )
    assert store.get_run(run)["status"] == "succeeded"
