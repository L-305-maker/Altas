"""Committed response survives real process death; governance is not reset."""

import asyncio
import json
import os
import subprocess
import sys

from sqlalchemy import update
from test_durable_runner_crash import database as database

from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import StepRow
from agentflow.infrastructure.persistence.store import Store


def policy():
    return ProviderAdmissionPolicy(
        provider_key="crash-usage",
        max_concurrent_requests=1,
        max_requests_per_window=20,
        window_seconds=60,
        policy_version="1",
    )


def worker(store, provider, runtime=None):
    agents = SQLAgentStore(store, admission_policy=policy())
    handlers = HandlerRegistry()

    async def handler(task):
        return await DurableAgentRunner(
            agents, provider, runtime or ToolRuntime(), admission=agents
        ).run(task, "public fixture", allowed={"echo"})

    handlers.register("agent", handler)
    return Worker(store, handlers, lease_seconds=300)


PROGRAM = """
import asyncio, os
from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.model_provider import ModelResponse
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.tools.runtime import Tool, ToolRuntime
from agentflow.domain.agent.status import ToolSideEffect
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store
store = Store(os.environ['USAGE_CRASH_DB'])
agents = SQLAgentStore(store, admission_policy=ProviderAdmissionPolicy.model_validate_json(os.environ['USAGE_CRASH_POLICY']))
commit = agents.commit_transition
def crash(task, transition):
    state = commit(task, transition)
    if state.snapshot.status == 'actions_pending':
        os._exit(13)
    return state
agents.commit_transition = crash
runtime = ToolRuntime()
async def tool(args):
    raise AssertionError('must not dispatch before injected exit')
runtime.register(Tool('echo', 'fixture', {'type': 'object'}, tool, side_effect=ToolSideEffect.READ_ONLY))
provider = MockProvider([ModelResponse(assistant_message={
    'role': 'assistant', 'tool_calls': [{'id': 'fixture-call', 'type': 'function',
    'function': {'name': 'echo', 'arguments': '{}'}}]},
    finish_reason='tool_calls', usage={'total_tokens': 5})])
handlers = HandlerRegistry()
async def handler(task):
    return await DurableAgentRunner(agents, provider, runtime, admission=agents).run(task, 'public fixture', allowed={'echo'})
handlers.register('agent', handler)
asyncio.run(Worker(store, handlers, lease_seconds=300).tick())
"""


def test_crash_restores_frozen_policy_usage_and_no_new_model_permit(
    database,
    advance_database_clock,
    runtime_invariants,
):
    store = Store(database)
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="usage-crash",
                steps=(
                    StepSpec(
                        id="work",
                        handler="agent",
                        parameters={
                            "agent": {
                                "mode": "durable_v1",
                                "observed_usage": {
                                    "max_observed_total_tokens": 5,
                                    "policy_version": "1",
                                },
                            }
                        },
                    ),
                ),
            )
        ),
        {},
    )
    store.close()
    child = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        env=dict(
            os.environ,
            USAGE_CRASH_DB=database,
            USAGE_CRASH_POLICY=json.dumps(policy().model_dump()),
        ),
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert child.returncode == 13
    store = Store(database)
    try:
        before = runtime_invariants(store, run)["work"].agent
        assert before.observed_usage.blocked and len(before.model_permits) == 1
        advance_database_clock(301)
        provider, invocations = MockProvider(), []

        async def tool(args):
            invocations.append(args)
            return {"value": 1}

        assert not asyncio.run(worker(store, provider).tick())
        with store.transaction() as db:
            db.execute(
                update(StepRow).where(StepRow.run_id == run).values(available_at=0)
            )
        runtime = ToolRuntime()
        runtime.register(
            Tool(
                "echo",
                "fixture",
                {"type": "object"},
                tool,
                side_effect=ToolSideEffect.READ_ONLY,
            )
        )
        assert asyncio.run(worker(store, provider, runtime).tick())
        after = runtime_invariants(store, run)["work"].agent
        assert after.snapshot.status == "exhausted" and provider.calls == 0
        assert after.model_attempts == before.model_attempts
        assert after.model_permits == before.model_permits
        assert len(invocations) == 1 and after.actions[0].action.status == "succeeded"
    finally:
        store.close()
