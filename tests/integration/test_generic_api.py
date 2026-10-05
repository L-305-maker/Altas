import asyncio

from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.store import Store
from agentflow.interfaces.api.app import create_app


def test_core_starts_without_applications(tmp_path):
    settings = Settings(
        api_token=SecretStr("test-generic-api-token"),
        provider="mock",
        tool_root=None,
        sandbox_enabled=False,
    )
    tools = ToolRuntime()
    handlers = build_handlers(settings, tools)
    assert handlers.names() == ("agent",)
    assert tools.schemas(set()) == []
    store = Store(f"sqlite:///{(tmp_path / 'core.db').as_posix()}")
    store.create_schema()
    try:
        empty = HandlerRegistry()
        with TestClient(create_app(settings, store, empty)) as client:
            client.headers["Authorization"] = "Bearer test-generic-api-token"
            assert client.get("/health").status_code == 200
            assert client.get("/api/meta").json()["handlers"] == []
            assert client.get("/api/workflows").json() == []
            assert client.post("/api/guidelines", json={}).status_code == 404
            assert "/api/guidelines" not in client.get("/openapi.json").json()["paths"]
            asyncio.run(Worker(store, empty).tick())
    finally:
        store.close()


def test_host_can_register_an_unrelated_application(tmp_path):
    settings = Settings(api_token=SecretStr("test-generic-api-token"), provider="mock")
    handlers = HandlerRegistry()

    async def research(task):
        return {"answer": task.inputs["question"]}

    handlers.register("research.answer", research)
    store = Store(f"sqlite:///{(tmp_path / 'research.db').as_posix()}")
    store.create_schema()
    try:
        with TestClient(create_app(settings, store, handlers)) as client:
            client.headers["Authorization"] = "Bearer test-generic-api-token"
            spec = WorkflowSpec(
                name="research",
                steps=(
                    StepSpec(
                        id="answer",
                        handler="research.answer",
                        requires_approval=True,
                        artifact=True,
                    ),
                ),
            )
            registered = client.post(
                "/api/workflows", json=spec.model_dump(mode="json")
            )
            assert registered.status_code == 201
            submitted = client.post(
                "/api/runs",
                json={
                    "workflow_id": registered.json()["id"],
                    "inputs": {"question": "hello"},
                },
            )
            assert submitted.status_code == 201
            run_id = submitted.json()["id"]
            detail = client.get(f"/api/runs/{run_id}").json()
            approval = detail["approvals"][0]["id"]
            assert (
                client.post(
                    f"/api/approvals/{approval}",
                    json={"approve": True, "actor": "tester"},
                ).status_code
                == 200
            )
            asyncio.run(Worker(store, handlers).tick())
            detail = client.get(f"/api/runs/{run_id}").json()
            assert detail["status"] == "succeeded"
            assert detail["artifacts"][0]["content"] == {"answer": "hello"}
            assert (
                "approval.approved"
                in client.get(f"/api/runs/{run_id}/events?follow=false").text
            )
            with client.websocket_connect(f"/api/runs/{run_id}/ws") as socket:
                socket.send_json({"token": "test-generic-api-token", "after": 0})
                assert socket.receive_json()["kind"] == "run.created"
    finally:
        store.close()
