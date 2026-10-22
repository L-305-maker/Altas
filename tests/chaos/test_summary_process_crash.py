"""Actual OS process loss at summary durable boundaries; synthetic sources only."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import update

from agentflow.infrastructure.persistence.models import StepRow

# Support this opt-in process test both standalone and in the release suite.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from test_durable_agent_runner import store as store  # noqa: E402
from test_durable_summary import (  # noqa: E402
    SummaryProvider,
    setup_summary,
    summary_worker,
)

PROGRAM = r"""
import asyncio, os, sys
sys.path.insert(0, 'tests/integration')
from test_durable_summary import SummaryProvider, summary_worker
from agentflow.infrastructure.persistence.store import Store
from agentflow.infrastructure.persistence.agent_store import SQLAgentStore
store = Store(os.environ['AGENTFLOW_SUMMARY_TEST_DB'])
agents = SQLAgentStore(store)
stage = os.environ['AGENTFLOW_SUMMARY_CRASH_STAGE']
original = agents.commit_transition
def commit(task, transition):
    saved = original(task, transition)
    if ((stage == 'model_reservation' and saved.snapshot.status == 'model_inflight')
        or (stage == 'tool_reservation' and any(a.status == 'inflight' for a in saved.snapshot.actions))
        or (stage == 'tool_result' and any(a.status == 'succeeded' for a in saved.snapshot.actions))):
        os._exit(13)
    return saved
agents.commit_transition = commit
complete = agents.complete_agent_step
def completed(*args):
    result = complete(*args)
    if stage == 'artifact':
        os._exit(13)
    return result
agents.complete_agent_step = completed
asyncio.run(summary_worker(store, agents, SummaryProvider()).tick())
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["model_reservation", "tool_reservation", "tool_result", "artifact"]
)
async def test_summary_real_process_crash_restores_only_persisted_facts(
    store, runtime_invariants, stage, advance_database_clock
):
    run, agents, _ = await setup_summary(store)
    source = runtime_invariants(store, run)["source"].agent
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", PROGRAM],
        cwd=Path(__file__).resolve().parents[2],
        env={
            **os.environ,
            "AGENTFLOW_SUMMARY_TEST_DB": store.engine.url.render_as_string(
                hide_password=False
            ),
            "AGENTFLOW_SUMMARY_CRASH_STAGE": stage,
        },
        capture_output=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert child.returncode == 13
    before = runtime_invariants(store, run)["summary"].agent
    with store.transaction() as db:
        db.execute(
            update(StepRow)
            .where(
                StepRow.run_id == run,
                StepRow.key == "summary",
                StepRow.status == "running",
            )
            .values(lease_until=0)
        )
    store.engine.dispose()
    provider = SummaryProvider()
    await summary_worker(store, agents, provider).tick()
    # Real lease failures retain the configured Step retry delay. Advance only
    # the isolated test DB clock; do not turn it into a normal continuation.
    advance_database_clock(2)
    await summary_worker(store, agents, provider).tick()
    after = runtime_invariants(store, run)["summary"].agent
    assert runtime_invariants(store, run)["source"].agent == source
    assert (
        after.model_attempts[0].context_projection
        == before.model_attempts[0].context_projection
    )
    if stage == "model_reservation":
        assert after.snapshot.status == "failed" and provider.calls == 0
        assert after.model_attempts[0].outcome == "unknown"
    elif stage == "tool_reservation":
        assert after.snapshot.status == "waiting_reconciliation" and provider.calls == 0
        assert after.actions[0].action.status == "unknown"
    else:
        assert after.snapshot.status == "succeeded" and len(store.artifacts(run)) == 2
        assert after.actions == before.actions
        assert provider.calls == (1 if stage == "tool_result" else 0)
