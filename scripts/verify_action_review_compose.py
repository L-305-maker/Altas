"""Run inside the API container after stopping its regular Worker (CI only).

Uses the real authenticated HTTP API and PostgreSQL, with a scripted provider and
generic protected tool. No fixture handler or approval bypass enters deployment.
"""

import asyncio
import json
import os
from urllib.request import Request, urlopen
from uuid import uuid4

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.config import Settings
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store


def api(path, payload=None):
    request = Request(
        os.environ.get("AGENTFLOW_VERIFY_BASE_URL", "http://127.0.0.1:8000/api").rstrip(
            "/"
        )
        + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ["AGENTFLOW_API_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=10) as response:
        return json.load(response)


async def verify(store, outcome):
    calls = []

    async def invoke(arguments, invocation):
        calls.append(invocation)
        if outcome == "unknown":
            raise TimeoutError
        return {"value": arguments["value"]}

    tools = ToolRuntime()
    tools.register(
        Tool(
            "write_record",
            "Write a generic record",
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            lambda args: None,
            requires_approval=True,
            side_effect=ToolSideEffect.IDEMPOTENT,
            invoke_durable=invoke,
            version="compose-v1",
        )
    )
    provider = MockProvider(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "provider-local",
                        "type": "function",
                        "function": {
                            "name": "write_record",
                            "arguments": '{"value":1}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "complete"},
        ]
    )
    agents, handlers = SQLAgentStore(store), HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(agents, provider, tools).run(
            task, "verify generic action", allowed={"write_record"}
        )

    handlers.register("agent", handler)
    workflow = api(
        "/workflows",
        {
            "name": "action-review-" + uuid4().hex,
            "steps": [
                {
                    "id": "work",
                    "handler": "agent",
                    "artifact": True,
                    "parameters": {"agent": {"mode": "durable_v1"}},
                }
            ],
        },
    )
    run_id = api("/runs", {"workflow_id": workflow["id"], "inputs": {}})["id"]
    worker = Worker(store, handlers)
    assert await worker.tick()
    approval = api(f"/agent/action-approvals?run_id={run_id}")[0]
    assert not calls and store.claim() is None
    session_id = approval["session_id"]
    revision = api(f"/agent/sessions/{session_id}/reviews")["revision"]
    decision = {
        "expected_revision": revision,
        "decision": "rejected" if outcome == "rejected" else "approved",
        "actor": "compose-smoke",
    }
    url = f"/agent/action-approvals/{approval['approval_id']}/decision"
    assert api(url, decision) == api(url, decision)
    assert await worker.tick()
    if outcome == "unknown":
        pending = api(f"/agent/reconciliations/pending?run_id={run_id}")[0]
        assert pending["action_id"] == approval["action_id"]
        assert api(f"/runs/{run_id}")["status"] == "waiting"
        assert not await worker.tick()
        api(
            f"/agent/sessions/{session_id}/actions/{pending['action_id']}/reconciliations",
            {
                "request_id": uuid4().hex,
                "expected_revision": pending["revision"],
                "confirmed_status": "succeeded",
                "result": {"value": 1},
                "evidence_reference": "external:compose-receipt",
                "actor": "compose-smoke",
            },
        )
        assert await worker.tick()
    detail = api(f"/runs/{run_id}")
    assert detail["status"] == "succeeded" and len(detail["artifacts"]) == 1
    assert len(calls) == (0 if outcome == "rejected" else 1)
    if calls:
        assert calls[0].action_id == approval["action_id"]
        assert (
            api(f"/agent/action-approvals/{approval['approval_id']}")["consumed_at"]
            is not None
        )
    assert provider.calls == 2
    assert len([m for m in provider.contexts[-1] if m["role"] == "tool"]) == 1
    print(f"action review HTTP/Worker/Artifact: {outcome} passed")


async def main():
    store = Store(Settings().database_url.get_secret_value())
    try:
        for outcome in ("approved", "rejected", "unknown"):
            await verify(store, outcome)
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
