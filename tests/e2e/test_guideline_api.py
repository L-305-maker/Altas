import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agentflow.application.orchestration.worker import Worker
from agentflow.bootstrap import build_handlers
from agentflow.config import Settings
from agentflow.infrastructure.persistence.store import Store
from agentflow.interfaces.api.app import create_app

TOKEN = "test-operator-token-not-a-secret"


@pytest.fixture
def system(tmp_path):
    store = Store(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    store.create_schema()
    settings = Settings(api_token=SecretStr(TOKEN), provider="mock")
    worker = Worker(store, build_handlers(settings))
    with TestClient(create_app(settings, store)) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client, worker, store
    store.close()


def test_evidence_to_approved_revision(system):
    client, worker, store = system
    body = {
        "title": "测试证据草稿",
        "documents": [{"title": "文档 A", "text": "这是原始证据，仅用于流程验证。"}],
    }
    created = client.post(
        "/api/guidelines", json=body, headers={"Idempotency-Key": "first"}
    )
    assert created.status_code == 201
    run_id = created.json()["id"]
    assert (
        client.post(
            "/api/guidelines", json=body, headers={"Idempotency-Key": "first"}
        ).json()["id"]
        == run_id
    )
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["status"] == "waiting"
    draft = next(s for s in detail["steps"] if s["id"] == "draft")["output"]
    assert draft["citations"][0]["quote"] in body["documents"][0]["text"]
    assert draft["mode"] == "mock"
    assert detail["artifacts"] == []
    approval = detail["approvals"][0]["id"]
    assert (
        client.post(
            f"/api/approvals/{approval}", json={"approve": True, "actor": "reviewer"}
        ).status_code
        == 200
    )
    asyncio.run(worker.tick())
    final = client.get(f"/api/runs/{run_id}").json()
    assert final["status"] == "succeeded"
    assert final["artifacts"][0]["content"]["revision"] == run_id
    events = client.get(f"/api/runs/{run_id}/events?follow=false")
    assert events.status_code == 200
    assert "approval.approved" in events.text
    cursor = store.events(run_id)[0]["id"]
    replay = client.get(
        f"/api/runs/{run_id}/events?follow=false",
        headers={"Last-Event-ID": str(cursor)},
    )
    assert f"id: {cursor}\n" not in replay.text


def test_auth_validation_and_request_limits(system):
    client, _, _ = system
    assert (
        client.get("/api/runs", headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert client.get("/health").status_code == 200
    assert (
        client.post(
            "/api/guidelines", json={"title": "empty", "documents": []}
        ).status_code
        == 422
    )
    assert client.post("/api/guidelines", content=b"x" * 2_000_001).status_code == 413
    assert client.get("/api/runs/missing").status_code == 404
    assert (
        client.post(
            "/api/workflows",
            json={"name": "bad", "steps": [{"id": "a", "handler": "unknown"}]},
        ).status_code
        == 422
    )


def test_cancelling_waiting_run_closes_approval(system):
    client, worker, _ = system
    run_id = client.post(
        "/api/guidelines",
        json={"title": "test", "documents": [{"title": "doc", "text": "source"}]},
    ).json()["id"]
    asyncio.run(worker.tick())
    asyncio.run(worker.tick())
    approval = client.get(f"/api/runs/{run_id}").json()["approvals"][0]["id"]
    assert client.post(f"/api/runs/{run_id}/cancel").json()["status"] == "cancelled"
    assert (
        client.post(
            f"/api/approvals/{approval}", json={"approve": True, "actor": "reviewer"}
        ).status_code
        == 409
    )


def test_websocket_authentication_and_replay(system):
    from starlette.websockets import WebSocketDisconnect

    client, _, store = system
    workflow_id = client.get("/api/workflows").json()[0]["id"]
    run_id = store.submit(workflow_id, {"title": "test", "documents": []})
    store.cancel(run_id)
    with client.websocket_connect(f"/api/runs/{run_id}/ws") as socket:
        socket.send_json({"token": TOKEN, "after": 0})
        assert socket.receive_json()["kind"] == "run.created"
    with client.websocket_connect(f"/api/runs/{run_id}/ws") as socket:
        socket.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as caught:
            socket.receive_json()
        assert caught.value.code == 1008
