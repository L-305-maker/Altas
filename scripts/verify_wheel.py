"""Install the wheel outside the checkout and exercise the reference application."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SMOKE = """
import asyncio
from fastapi.testclient import TestClient
from pydantic import SecretStr
from agentflow.application.orchestration.worker import Worker
from agentflow.config import Settings
from agentflow.infrastructure.persistence.store import Store
from agentflow.bootstrap import build_handlers as core_handlers
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.infrastructure.persistence.models import AgentSessionRow
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.interfaces.api.app import create_app as core_app
from sqlalchemy import select
from applications.living_guideline.bootstrap import build_handlers, create_app

settings = Settings(provider="mock", api_token=SecretStr("wheel-smoke-test-token"))
store = Store("sqlite:///smoke.db")
store.create_schema()
try:
    with TestClient(create_app(settings, store)) as client:
        client.headers["Authorization"] = "Bearer wheel-smoke-test-token"
        response = client.post("/api/guidelines", json={"title": "wheel", "documents": [{"title": "doc", "text": "evidence"}]})
        assert response.status_code == 201
        run_id = response.json()["id"]
        worker = Worker(store, build_handlers(settings))
        asyncio.run(worker.tick())
        asyncio.run(worker.tick())
        approval = store.approvals(run_id)[0]["id"]
        assert client.post(f"/api/approvals/{approval}", json={"approve": True, "actor": "wheel-test"}).status_code == 200
        asyncio.run(worker.tick())
        assert store.get_run(run_id)["status"] == "succeeded"
        assert store.artifacts(run_id)[0]["content"]["citations"][0]["quote"] == "evidence"
    # Exercise installed core modules and the existing authenticated metrics endpoint.
    observed = Settings(provider="mock", telemetry=True, api_token=SecretStr("wheel-smoke-test-token"), provider_admission=ProviderAdmissionPolicy(
        provider_key="wheel-fixture", max_concurrent_requests=1, max_requests_per_window=2, window_seconds=60, policy_version="1",
    ), observed_usage=ObservedUsagePolicy(max_observed_total_tokens=100, missing_usage_policy="continue_with_warning", policy_version="1"))
    with TestClient(core_app(observed, store)) as client:
        run_id = store.submit(store.register(WorkflowSpec(name="installed-durable", steps=(
            StepSpec(id="agent", handler="agent", artifact=True, parameters={"prompt":"wheel goal", "agent":{"mode":"durable_v1", "context_projection":{"mode":"recent_complete_rounds"}}}),
        ))), {})
        worker = Worker(store, core_handlers(observed, store=store), observe=store.observe_execution)
        assert asyncio.run(worker.tick())
        assert store.get_run(run_id)["status"] == "succeeded"
        assert len(store.artifacts(run_id)) == 1
        with store.transaction() as db:
            snapshot = AgentSnapshot.model_validate(db.scalar(select(AgentSessionRow)).snapshot)
        assert snapshot.status == "succeeded" and snapshot.budget.model_attempts == 1
        summary = SQLAgentStore(store).usage_summary(snapshot.id)
        assert summary.attempts == summary.unavailable_usage_attempts == 1
        assert summary.total_tokens.value is None
        with SQLAgentStore(store)._operator_transaction(snapshot.id) as (_, _, _, _, session):
            assert len(session.model_permits) == 1 and session.model_permits[0].status == "released"
            assert session.observed_usage.partial and session.observed_usage.summary.total_tokens.value is None
            assert session.model_attempts[0].context_projection.selection_policy == "recent_complete_rounds"
            contract = session.model_attempts[0].request_contract
            assert contract.model == "mock-v1" and contract.max_output_tokens == 4096
        assert client.get("/metrics").status_code == 401
        response = client.get("/metrics", headers={"Authorization":"Bearer wheel-smoke-test-token"})
        assert response.status_code == 200
        assert 'agentflow_agent_model_requests_total{outcome="reserved"} 1.0' in response.text
finally:
    store.close()
from applications.living_guideline.evaluation import main
asyncio.run(main())
print("Installed wheel: reference application, durable runtime and metrics passed")
"""


def main():
    wheel = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="agentflow-wheel-") as directory:
        root = Path(directory)
        venv = root / "venv"
        subprocess.run(
            ["uv", "venv", str(venv), "--python", sys.executable],
            check=True,
            timeout=60,
        )
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)],
            check=True,
            timeout=180,
        )
        subprocess.run(
            [str(python), "-I", "-c", SMOKE], cwd=root, check=True, timeout=120
        )


if __name__ == "__main__":
    main()
