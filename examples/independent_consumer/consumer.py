"""Independent, offline-only business fixture. No application or ORM imports."""

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from pydantic import SecretStr

from agentflow.runtime import (
    DurableAgentRunner,
    HandlerRegistry,
    MockProvider,
    ModelResponse,
    Settings,
    SQLAgentStore,
    StepSpec,
    Store,
    Tool,
    ToolRuntime,
    ToolSideEffect,
    Worker,
    WorkflowAdmissionPolicy,
    WorkflowSpec,
    create_app,
)


class ConsumerProvider(MockProvider):
    """Deterministic request-aware Mock; recovery reads persisted messages."""

    async def respond(self, messages, tools=None):
        if any(m["role"] == "tool" for m in messages):
            message = {"role": "assistant", "content": "record confirmed"}
        else:
            context = json.loads(
                next(m["content"] for m in messages if m["role"] == "user")
            )
            message = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "record",
                        "type": "function",
                        "function": {
                            "name": "record",
                            "arguments": json.dumps(context["dependencies"]["prepare"]),
                        },
                    }
                ],
            }
        return ModelResponse(
            assistant_message=message,
            finish_reason="tool_calls" if message.get("tool_calls") else "stop",
        )


def workflow():
    return WorkflowSpec(
        name="independent-consumer",
        steps=(
            StepSpec(id="prepare", handler="independent.prepare.v1", retry_delay=0),
            StepSpec(
                id="agent",
                handler="independent.agent.v1",
                depends_on=("prepare",),
                artifact=True,
                retry_delay=0,
                parameters={"agent": {"mode": "durable_v1"}},
            ),
            StepSpec(
                id="audit",
                handler="independent.audit.v1",
                depends_on=("agent",),
                artifact=True,
            ),
        ),
    )


def components():
    store = Store(
        os.environ["INDEPENDENT_CONSUMER_DB_URL"],
        workflow_admission=WorkflowAdmissionPolicy(max_active_runs=2),
    )
    agents = SQLAgentStore(store)
    handlers = HandlerRegistry()

    async def prepare(task):
        return {"record": task.inputs["record"]}

    async def agent(task):
        # Custom handlers own their dispatch semantics; a mode label alone is no proof.
        if task.spec.parameters.get("agent", {}).get("mode") != "durable_v1":
            raise ValueError("ConsumerDurableModeRequired")
        tools = ToolRuntime()

        async def legacy(_arguments):
            raise AssertionError("LegacyToolPath")

        async def record(arguments, invocation):
            path = Path(os.environ["INDEPENDENT_CONSUMER_RECORDS"]) / "effects.jsonl"
            # This ledger simulates an external side effect, outside the Core transaction.
            with path.open("ab") as stream:
                stream.write(
                    (
                        json.dumps(
                            {
                                "action": invocation.action_id,
                                "key": invocation.idempotency_key,
                                "record": arguments["record"],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                stream.flush()
                os.fsync(stream.fileno())
            if task.inputs.get("crash_after_dispatch"):
                os._exit(
                    73
                )  # Owned fixture process: effect happened, confirmation did not.
            return {"recorded": True}

        tools.register(
            Tool(
                "record",
                "Record one synthetic item",
                {
                    "type": "object",
                    "properties": {"record": {"type": "string", "maxLength": 80}},
                    "required": ["record"],
                    "additionalProperties": False,
                },
                legacy,
                requires_approval=True,
                side_effect=ToolSideEffect.UNSAFE,
                invoke_durable=record,
            )
        )
        return await DurableAgentRunner(agents, ConsumerProvider(), tools).run(
            task, "Record the approved synthetic item", allowed={"record"}
        )

    async def audit(task):
        return {
            "consumer": "independent",
            "schema_version": 1,
            "agent_result": task.dependencies["agent"],
        }

    for name, handler in (("prepare", prepare), ("agent", agent), ("audit", audit)):
        handlers.register(f"independent.{name}.v1", handler)
    return store, agents, handlers


def api(store, handlers):
    settings = Settings(
        provider="mock",
        api_token=SecretStr(os.environ["INDEPENDENT_CONSUMER_API_TOKEN"]),
    )
    return create_app(settings, store, handlers)


async def work(run_id):
    store, _, handlers = components()
    try:
        worker = Worker(store, handlers, lease_seconds=1, concurrency=1)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await worker.tick()
            if store.get_run(run_id)["status"] in {
                "waiting",
                "succeeded",
                "failed",
                "cancelled",
            }:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("ConsumerProgressTimeout")
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    args = parser.parse_args()
    asyncio.run(work(args.run_id))
