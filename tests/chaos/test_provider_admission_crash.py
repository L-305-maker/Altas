"""Actual process exit after admission, dispatch or durable quota deferral."""

import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import update
from test_durable_runner_crash import database as database

from agentflow.application.agents.durable_runner import (
    DurableAgentConfig,
    DurableAgentRunner,
)
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.agent_store import AgentTransition
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.models import StepRow
from agentflow.infrastructure.persistence.store import Store

POLICY = {
    "provider_key": "crash-provider",
    "max_concurrent_requests": 1,
    "max_requests_per_window": 20,
    "window_seconds": 60,
    "admission_retry_delay_seconds": 2,
    "permit_ttl_seconds": 120,
    "policy_version": "1",
}

PROGRAM = """
import asyncio, json, os
from pathlib import Path
from agentflow.application.agents.durable_runner import DurableAgentRunner
from agentflow.application.orchestration.worker import HandlerRegistry, Worker
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.application.tools.runtime import ToolRuntime
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
from agentflow.infrastructure.persistence.store import Store
store = Store(os.environ['ADMISSION_CRASH_DB'])
agents = SQLAgentStore(store, admission_policy=ProviderAdmissionPolicy.model_validate_json(os.environ['ADMISSION_CRASH_POLICY']))
stage = os.environ['ADMISSION_CRASH_STAGE']
reserve = agents.reserve_model_attempt
def crash_after_reservation(*args):
    result = reserve(*args)
    if stage in {'reserved', 'deferred'}:
        os._exit(13)
    return result
agents.reserve_model_attempt = crash_after_reservation
class Provider(MockProvider):
    async def respond(self, messages, tools=None):
        Path(os.environ['ADMISSION_CRASH_COUNT']).write_text('1')
        os._exit(13)
handlers = HandlerRegistry()
async def handler(task):
    return await DurableAgentRunner(agents, Provider(), ToolRuntime(), admission=agents).run(task, 'public fixture', allowed=set())
handlers.register('agent', handler)
asyncio.run(Worker(store, handlers).tick())
"""


@pytest.mark.parametrize("stage", ["reserved", "dispatched", "deferred"])
def test_process_crash_keeps_permit_window_and_never_replays_unknown(
    database, tmp_path, runtime_invariants, advance_database_clock, stage
):
    import json

    store = Store(database)
    config = ProviderAdmissionPolicy(**POLICY)
    agents = SQLAgentStore(store, admission_policy=config)
    spec = WorkflowSpec(
        name="admission-crash",
        steps=(
            StepSpec(
                id="work",
                handler="agent",
                retry_delay=0,
                parameters={"agent": {"mode": "durable_v1"}},
            ),
        ),
    )
    if stage == "deferred":
        store.submit(store.register(spec), {})
        owner = store.claim(300)
        ready = agents.open(
            owner,
            AgentSnapshot(id=uuid4().hex, budget=DurableAgentConfig().initial_budget()),
        )
        snapshot = AgentSnapshot.model_validate(
            {
                **ready.snapshot.model_dump(),
                "revision": 1,
                "status": "model_inflight",
                "model_call_id": uuid4().hex,
                "budget": ready.snapshot.budget.consume(
                    model_rounds=1, model_attempts=1
                ),
            }
        )
        agents.reserve_model_attempt(owner, AgentTransition(snapshot=snapshot))
    run = store.submit(store.register(spec), {})
    store.close()
    counter = tmp_path / "request-count"
    counter.write_text("0")
    child = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        env=dict(
            os.environ,
            ADMISSION_CRASH_DB=database,
            ADMISSION_CRASH_POLICY=json.dumps(POLICY),
            ADMISSION_CRASH_STAGE=stage,
            ADMISSION_CRASH_COUNT=str(counter),
        ),
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert child.returncode == 13
    store = Store(database)
    try:
        before = runtime_invariants(store, run)["work"]
        provider = MockProvider()
        agents = SQLAgentStore(store, admission_policy=config)
        handlers = HandlerRegistry()

        async def handler(task):
            return await DurableAgentRunner(
                agents, provider, ToolRuntime(), admission=agents
            ).run(task, "public fixture", allowed=set())

        handlers.register("agent", handler)
        if stage == "deferred":
            assert before.agent.snapshot.budget.model_attempts == 0
            assert before.agent.model_permits == ()
            store.engine.dispose()
            assert store.claim(300) is None
            assert (
                runtime_invariants(store, run)["work"].step["available_at"]
                == before.step["available_at"]
            )
            advance_database_clock(121)
            assert asyncio.run(Worker(store, handlers).tick())
            after = runtime_invariants(store, run)["work"].agent
            assert after.snapshot.status == "succeeded" and provider.calls == 1
        else:
            assert before.agent.snapshot.budget.model_attempts == 1
            assert before.agent.model_permits[0].status == "active"
            with store.transaction() as db:
                db.execute(
                    update(StepRow).where(StepRow.run_id == run).values(lease_until=0)
                )
            assert asyncio.run(Worker(store, handlers).tick())
            after = runtime_invariants(store, run)["work"].agent
            assert after.snapshot.status == "failed" and provider.calls == 0
            assert after.model_attempts[0].outcome == "unknown"
            assert after.model_permits[0].status == "unknown"
            assert after.model_permits[0].id == before.agent.model_permits[0].id
            assert (
                after.model_permits[0].expires_at
                == before.agent.model_permits[0].expires_at
            )
        assert int(counter.read_text()) == int(stage == "dispatched")
    finally:
        store.close()
