import asyncio

import pytest
from fastapi.testclient import TestClient

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import AgentStoreUnavailable
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.config import Settings
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store
from agentflow.interfaces.api.app import create_app


@pytest.fixture
def scenario(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'api-review.db').as_posix()}")
    store.create_schema()
    agents = SQLAgentStore(store)
    invoked = []
    tools = ToolRuntime()

    async def tool(args):
        invoked.append(args)
        if args.get("unknown"):
            raise TimeoutError
        return {"value": 1}

    tools.register(
        Tool(
            "write",
            "write",
            {"type": "object"},
            tool,
            requires_approval=True,
            side_effect=ToolSideEffect.IDEMPOTENT,
        )
    )
    provider = MockProvider()
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(agents, provider, tools).run(
            task, "goal", allowed={"write"}
        )

    handlers.register("agent", handler)
    with TestClient(
        create_app(Settings(api_token="test-review-operator-token"), store, handlers)
    ) as client:
        yield client, store, agents, provider, invoked, handlers
    store.close()


def start(scenario, unknown=False):
    client, store, agents, provider, invoked, handlers = scenario
    provider.responses = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "provider",
                    "type": "function",
                    "function": {
                        "name": "write",
                        "arguments": '{"unknown":true}' if unknown else "{}",
                    },
                }
            ],
        },
        {"role": "assistant", "content": "done"},
    ]
    client.headers["Authorization"] = "Bearer test-review-operator-token"
    workflow = client.post(
        "/api/workflows",
        json=WorkflowSpec(
            name="api-review",
            steps=(StepSpec(id="work", handler="agent", artifact=True),),
        ).model_dump(mode="json"),
    ).json()["id"]
    run = client.post("/api/runs", json={"workflow_id": workflow}).json()["id"]
    assert asyncio.run(Worker(store, handlers).tick())
    return run


def test_api_approval_worker_artifact_e2e(scenario):
    client, store, agents, provider, invoked, handlers = scenario
    run = start(scenario)
    pending = client.get("/api/agent/action-approvals", params={"run_id": run}).json()[
        0
    ]
    assert not invoked
    assert (
        client.get(f"/api/agent/action-approvals/{pending['approval_id']}").json()
        == pending
    )
    history = client.get(f"/api/agent/sessions/{pending['session_id']}/reviews").json()
    body = {
        "expected_revision": history["revision"],
        "decision": "approved",
        "actor": "operator-label",
        "reason": "checked",
    }
    url = f"/api/agent/action-approvals/{pending['approval_id']}/decision"
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json={**body, "decision": "rejected"}).status_code == 409
    asyncio.run(Worker(store, handlers).tick())
    assert len(invoked) == 1
    detail = client.get(f"/api/runs/{run}").json()
    assert detail["status"] == "succeeded" and len(detail["artifacts"]) == 1


def test_api_unknown_reconciliation_resume_e2e(scenario):
    client, store, agents, provider, invoked, handlers = scenario
    run = start(scenario, unknown=True)
    approval = client.get("/api/agent/action-approvals").json()[0]
    revision = client.get(
        f"/api/agent/sessions/{approval['session_id']}/reviews"
    ).json()["revision"]
    client.post(
        f"/api/agent/action-approvals/{approval['approval_id']}/decision",
        json={
            "expected_revision": revision,
            "decision": "approved",
            "actor": "operator",
        },
    )
    asyncio.run(Worker(store, handlers).tick())
    pending = client.get("/api/agent/reconciliations/pending").json()[0]
    url = f"/api/agent/sessions/{pending['session_id']}/actions/{pending['action_id']}/reconciliations"
    body = {
        "request_id": "receipt",
        "expected_revision": pending["revision"],
        "confirmed_status": "succeeded",
        "result": {"value": 1},
        "evidence_reference": "external:receipt",
        "actor": "operator-label",
    }
    assert client.post(url, json={**body, "expected_revision": 0}).status_code == 409
    assert client.post(url, json={**body, "evidence_reference": ""}).status_code == 422
    assert client.post(url, json=body).status_code == 200
    assert client.post(url, json=body).status_code == 200
    asyncio.run(Worker(store, handlers).tick())
    assert len(invoked) == 1 and store.get_run(run)["status"] == "succeeded"
    history = client.get(f"/api/agent/sessions/{pending['session_id']}/reviews").json()
    assert len(history["reconciliations"]) == 1
    assert len([m for m in provider.contexts[-1] if m["role"] == "tool"]) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/agent/action-approvals"),
        ("get", "/api/agent/action-approvals/missing"),
        ("get", "/api/agent/reconciliations/pending"),
        ("get", "/api/agent/sessions/missing/reviews"),
        ("post", "/api/agent/action-approvals/missing/decision"),
        ("post", "/api/agent/sessions/missing/actions/missing/reconciliations"),
    ],
)
def test_review_endpoints_require_authentication(scenario, method, path):
    client, *_ = scenario
    body = (
        {"expected_revision": 0, "decision": "approved", "actor": "operator"}
        if "decision" in path
        else {
            "request_id": "r",
            "expected_revision": 0,
            "confirmed_status": "succeeded",
            "actor": "operator",
            "evidence_reference": "evidence",
        }
    )
    assert (
        getattr(client, method)(
            path, **({"json": body} if method == "post" else {})
        ).status_code
        == 401
    )


def test_review_database_unavailable_is_safe_503(scenario, monkeypatch):
    client, *_ = scenario
    client.headers["Authorization"] = "Bearer test-review-operator-token"

    def failed(*args, **kwargs):
        raise AgentStoreUnavailable("safe unavailable")

    monkeypatch.setattr(SQLAgentStore, "list_pending_action_approvals", failed)
    assert client.get("/api/agent/action-approvals").status_code == 503
