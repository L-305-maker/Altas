"""Characterize the existing oldest-eligible-Run ordering, not an SLO promise."""

import os

import pytest
from sqlalchemy import select
from test_postgres import pgstore as pgstore

from agentflow.infrastructure.persistence.models import RunRow
from agentflow.runtime import StepSpec, Store, WorkflowSpec

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTFLOW_TEST_POSTGRES_URL"),
    reason="requires a dedicated PostgreSQL test database",
)


def test_postgres_large_old_run_precedes_continuously_added_short_runs(pgstore):
    old = pgstore.submit(
        pgstore.register(
            WorkflowSpec(
                name="old",
                steps=tuple(StepSpec(id=f"s{i}", handler="echo") for i in range(100)),
            )
        ),
        {},
    )
    small = pgstore.register(
        WorkflowSpec(name="short", steps=(StepSpec(id="one", handler="echo"),))
    )
    later = []
    for _ in range(100):
        later.append(pgstore.submit(small, {}))
        task = pgstore.claim(300)
        assert task.run_id == old
        pgstore.complete(task, {})
    assert pgstore.claim(300).run_id == later[0]


def test_postgres_locked_old_run_does_not_block_another_engine(pgstore):
    wid = pgstore.register(
        WorkflowSpec(name="locked", steps=(StepSpec(id="one", handler="echo"),))
    )
    old, new = pgstore.submit(wid, {}), pgstore.submit(wid, {})
    peer = Store(pgstore.engine.url.render_as_string(hide_password=False))
    try:
        with pgstore.transaction() as db:
            db.scalar(select(RunRow).where(RunRow.id == old).with_for_update())
            task = peer.claim(300)
            assert task.run_id == new
            peer.complete(task, {})
        assert peer.claim(300).run_id == old
    finally:
        peer.close()
