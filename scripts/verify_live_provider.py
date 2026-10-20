"""One explicitly authorized paid DeepSeek request, using only a fixed public prompt.

No hidden retries: max_model_attempts=1. Existing provider caps output at 4096
tokens, request timeout 45s. Input is fixed and below 1KB. A monetary cap must be
enforced on the dedicated provider account; this test cannot infer changing prices
or prevent billing for an UNKNOWN request. No response text/IDs/usage values print.
"""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.model_request import PreparedModelRequest
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.deepseek import DeepSeekProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import AgentSessionRow
from agentflow.infrastructure.persistence.store import Store


def authorized():
    return os.environ.get(
        "AGENTFLOW_LIVE_DEEPSEEK_AUTHORIZATION"
    ) == "one-paid-request" and bool(os.environ.get("AGENTFLOW_DEEPSEEK_API_KEY"))


async def verify(provider, url):
    metadata, calls = {}, 0

    class Once:
        def prepare_request(self, messages, tools, projection):
            if calls or tools:
                raise ValueError("LiveRequestLimit")
            prepared = provider.prepare_request(messages, tools, projection)

            async def execute():
                nonlocal calls
                if calls:
                    raise ValueError("LiveRequestLimit")
                calls += 1
                response = await prepared.execute()
                assert response.assistant_message.role == "assistant"
                assert response.finish_reason == "stop"
                assert (
                    response.assistant_message.content
                    and not response.assistant_message.tool_calls
                )
                metadata.update(
                    usage_present=response.usage is not None,
                    request_id_present=response.request_id is not None,
                    model_present=response.model is not None,
                )
                return response

            return PreparedModelRequest(prepared.request, execute)

    store = Store(url)
    try:
        store.create_schema()
        run = store.submit(
            store.register(
                WorkflowSpec(
                    name="live-public-smoke",
                    steps=(
                        StepSpec(
                            id="agent",
                            handler="live",
                            artifact=True,
                            max_attempts=1,
                            timeout=60,
                            parameters={
                                "agent": {
                                    "max_model_attempts": 1,
                                    "max_model_rounds": 1,
                                    "max_tool_calls": 0,
                                }
                            },
                        ),
                    ),
                )
            ),
            {},
        )
        handlers = HandlerRegistry()

        async def handler(task):
            return await DurableAgentRunner(
                SQLAgentStore(store), Once(), ToolRuntime()
            ).run(task, "Reply with READY. Do not request any tools.", allowed=set())

        handlers.register("live", handler)
        async with asyncio.timeout(65):
            assert await Worker(store, handlers, lease_seconds=15).tick()
        assert store.get_run(run)["status"] == "succeeded"
        assert len(store.artifacts(run)) == 1
        with store.transaction() as db:
            snapshot = db.scalar(select(AgentSessionRow)).snapshot
        assert snapshot["budget"]["model_attempts"] == 1
        return {
            "status": "PASS",
            "requests": calls,
            "finish_reason": "stop",
            **metadata,
        }
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid-request", action="store_true")
    args = parser.parse_args()
    if not args.allow_paid_request or not authorized():
        print(json.dumps({"status": "LIVE PROVIDER NOT VERIFIED", "requests": 0}))
        return 1 if args.allow_paid_request else 0
    try:
        # Fixed HTTPS endpoint/model: environment cannot redirect the credential.
        provider = DeepSeekProvider(
            os.environ["AGENTFLOW_DEEPSEEK_API_KEY"], "deepseek-chat"
        )
        with tempfile.TemporaryDirectory(prefix="agentflow-live-") as directory:
            report = asyncio.run(
                verify(
                    provider, f"sqlite:///{(Path(directory) / 'live.db').as_posix()}"
                )
            )
        print(json.dumps(report))
        return 0
    except Exception:
        # No traceback, exception message, prompt, key, response, or provider ID.
        print(json.dumps({"status": "FAIL", "error": "LiveProviderAcceptanceFailed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
