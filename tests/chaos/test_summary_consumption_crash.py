"""Real OS process loss at consumer reservations and committed observations."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import update

from agentflow.infrastructure.persistence.models import StepRow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from test_durable_agent_runner import final  # noqa: E402
from test_durable_agent_runner import store as store  # noqa: E402
from test_summary_consumption import consumer_fixture, consumer_worker  # noqa: E402

from agentflow.infrastructure.models.mock import MockProvider  # noqa: E402

PROGRAM = r"""
import asyncio, os, sys
sys.path.insert(0, 'tests/integration')
from test_durable_agent_runner import final
from test_summary_consumption import consumer_worker, read_summary
from agentflow.infrastructure.models.mock import MockProvider
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
store = Store(os.environ['AGENTFLOW_CONSUMER_TEST_DB'])
agents = SQLAgentStore(store)
stage = os.environ['AGENTFLOW_CONSUMER_CRASH_STAGE']
original = agents.commit_transition
def commit(task, transition):
    saved = original(task, transition)
    if ((stage == 'model' and saved.snapshot.status == 'model_inflight')
        or (stage == 'tool' and any(a.status == 'inflight' for a in saved.snapshot.actions))
        or (stage == 'result' and any(a.status == 'succeeded' for a in saved.snapshot.actions))):
        os._exit(13)
    return saved
agents.commit_transition = commit
complete = agents.complete_agent_step
def completed(*args):
    saved = complete(*args)
    if stage == 'complete': os._exit(13)
    return saved
agents.complete_agent_step = completed
asyncio.run(consumer_worker(store, agents, MockProvider([read_summary(), final('done')])).tick())
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["model", "tool", "result", "complete"])
async def test_consumer_process_loss_never_replays_unknown_or_completed_reads(
    store, runtime_invariants, advance_database_clock, stage
):
    run, agents = await consumer_fixture(store)
    original = runtime_invariants(store, run)
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", PROGRAM],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "AGENTFLOW_CONSUMER_TEST_DB": store.engine.url.render_as_string(
                hide_password=False
            ),
            "AGENTFLOW_CONSUMER_CRASH_STAGE": stage,
        },
        capture_output=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert child.returncode == 13
    before = runtime_invariants(store, run)["next"].agent
    with store.transaction() as db:
        db.execute(
            update(StepRow)
            .where(
                StepRow.run_id == run,
                StepRow.key == "next",
                StepRow.status == "running",
            )
            .values(lease_until=0)
        )
    store.engine.dispose()
    provider = MockProvider([final("done")])
    await consumer_worker(store, agents, provider).tick()
    advance_database_clock(2)
    await consumer_worker(store, agents, provider).tick()
    states = runtime_invariants(store, run)
    after = states["next"].agent
    assert states["source"].agent == original["source"].agent
    assert states["summary"].agent == original["summary"].agent
    assert (
        after.model_attempts[0].request_contract
        == before.model_attempts[0].request_contract
    )
    if stage == "model":
        assert provider.calls == 0 and after.model_attempts[0].outcome == "unknown"
        assert after.snapshot.status == "failed"
    elif stage == "tool":
        assert provider.calls == 0 and after.snapshot.status == "waiting_reconciliation"
        assert after.actions[0].action.status == "unknown"
    else:
        assert after.snapshot.status == "succeeded" and after.actions == before.actions
        assert provider.calls == (1 if stage == "result" else 0)
        assert len(store.artifacts(run)) == 2
