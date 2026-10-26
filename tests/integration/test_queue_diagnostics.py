"""Queue facts remain read-only, bounded, aggregate, and separate from counters."""

import json

import pytest
from sqlalchemy import event, text, update
from test_durable_agent_runner import store as store

from agentflow.infrastructure.persistence.models import StepRow
from agentflow.runtime import StepSpec, WorkflowSpec


def test_queue_snapshot_counts_ready_delayed_leased_waiting_and_cancelled(store):
    def submit(name, **step):
        return store.submit(
            store.register(
                WorkflowSpec(
                    name=name, steps=(StepSpec(id="work", handler="echo", **step),)
                )
            ),
            {"private": "PRIVATE"},
        )

    leased = submit("leased")
    old = store.claim(300)
    delayed = submit("delayed", retry_delay=3600)
    task = store.claim(60)
    store.fail(task, RuntimeError("PRIVATE"))
    ready = submit("ready")
    submit("approval", requires_approval=True)
    cancelled = submit("cancelled")
    store.cancel(cancelled)
    with store.transaction() as db:
        db.execute(
            update(StepRow).where(StepRow.run_id == leased).values(lease_until=0)
        )
    statements = []

    def observe(_conn, _cursor, statement, *_):
        statements.append(statement.upper())

    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        report = store.queue_diagnostics()
    finally:
        event.remove(store.engine, "before_cursor_execute", observe)
    assert report["active_runs"] == 4
    assert report["ready_steps"] == report["delayed_steps"] == 1
    assert report["leased_steps"] == report["waiting_approval"] == 1
    assert report["expired_leases"] == 1
    assert report["waiting_reconciliation"] == 0
    assert report["oldest_ready_wait_seconds"] >= 0
    assert report["admission_rejections_observed"] is None
    assert report["scope"]["kind"] == "database_snapshot"
    encoded = json.dumps(report)
    assert all(
        private not in encoded
        for private in ("PRIVATE", ready, delayed, leased, old.token)
    )
    assert not any(
        s.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE"))
        for s in statements
    )
    if store.engine.dialect.name == "postgresql":
        assert any("READ ONLY" in s for s in statements)
        assert any("STATEMENT_TIMEOUT" in s for s in statements)
    # Read-only protection is reset on the pooled connection.
    store.cancel(ready)
    assert store.queue_diagnostics()["active_runs"] == 3


def test_diagnostics_bounds_are_explicit_and_database_failure_is_not_empty(store):
    for seconds in (0, 61, True):
        with pytest.raises(ValueError):
            store.queue_diagnostics(timeout_seconds=seconds)
    if store.engine.dialect.name == "postgresql":
        from sqlalchemy.exc import DBAPIError

        with store.engine.connect() as db:
            with pytest.raises(DBAPIError):
                db.execute(text("SELECT 1/0"))
    assert store.queue_diagnostics()["active_runs"] == 0


def test_queue_statement_timeout_is_not_an_empty_snapshot(store):
    if store.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL statement and table locks require PostgreSQL")
    from sqlalchemy.exc import DBAPIError

    with store.transaction() as db:
        db.execute(text("LOCK TABLE steps IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DBAPIError):
            store.queue_diagnostics(timeout_seconds=1)
    assert store.queue_diagnostics()["active_runs"] == 0
