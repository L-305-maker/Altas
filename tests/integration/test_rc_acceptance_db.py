import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, select
from test_durable_agent_runner import store as store

from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import ArtifactRow, StepRow


def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import rc_acceptance_db

    return rc_acceptance_db


def failures(snapshot):
    from rc_acceptance import snapshot_failures

    return snapshot_failures(snapshot)


def test_snapshot_is_readonly_bounded_and_does_not_publish_content(store, monkeypatch):
    m = module(monkeypatch)
    batch = "11111111-2222-4333-8444-555555555555"
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="rc-final-1",
                steps=(StepSpec(id="PRIVATE_STEP", handler="plain", artifact=True),),
            )
        ),
        {"scenario": "final", "rc_staging_batch": batch},
    )
    task = store.claim(30)
    assert task is not None
    assert store.complete(task, {"PRIVATE_TOOL_RESULT": "PRIVATE_PROMPT"})
    plan = {
        "environment": batch,
        "batch_id": batch,
        "runs": [{"id": run, "scenario": "final"}],
    }
    statements = []

    def observe(conn, cursor, statement, *args):
        statements.append(statement.upper())

    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        snapshot = m.collect_snapshot(store.engine, plan, batch)
    finally:
        event.remove(store.engine, "before_cursor_execute", observe)
    output = snapshot.model_dump_json()
    assert "PRIVATE" not in output and run not in output and task.token not in output
    assert len(snapshot.steps) == len(snapshot.artifacts) == 1
    assert failures(snapshot) == ()
    assert not any(
        s.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"))
        for s in statements
    )
    assert not any("FOR UPDATE" in s for s in statements)
    if store.engine.dialect.name == "postgresql":
        assert any("REPEATABLE READ, READ ONLY" in s for s in statements)
    with store.transaction() as db:
        row = db.scalar(select(ArtifactRow).where(ArtifactRow.run_id == run))
        assert row.content == {"PRIVATE_TOOL_RESULT": "PRIVATE_PROMPT"}
    with pytest.raises(ValueError, match="StagingPlanRejected"):
        m.collect_snapshot(
            store.engine,
            {**plan, "batch_id": "22222222-2222-4333-8444-555555555555"},
            batch,
        )


def test_persistent_snapshot_corruption_is_a_failure_not_an_empty_report(
    store, monkeypatch
):
    m = module(monkeypatch)
    batch = "11111111-2222-4333-8444-555555555555"
    run = store.submit(
        store.register(
            WorkflowSpec(
                name="rc-final-1", steps=(StepSpec(id="agent", handler="plain"),)
            )
        ),
        {"scenario": "final", "rc_staging_batch": batch},
    )
    plan = {
        "environment": batch,
        "batch_id": batch,
        "runs": [{"id": run, "scenario": "final"}],
    }
    with store.transaction() as db:
        db.scalar(select(StepRow).where(StepRow.run_id == run)).status = "PRIVATE_BAD"
    snapshot = m.collect_snapshot(store.engine, plan, batch)
    assert snapshot.invalid_records == 1
    assert "InvalidPersistentState" in failures(snapshot)
    assert "PRIVATE" not in json.dumps(snapshot.model_dump(mode="json"))
    assert snapshot.observed_at <= datetime.now(timezone.utc)


@pytest.mark.parametrize("scenario", ["unknown", "summary_consumption"])
def test_snapshot_verifies_real_review_and_summary_history(
    store, monkeypatch, tmp_path, scenario
):
    m = module(monkeypatch)
    from rc_workload import Workload

    batch = "11111111-2222-4333-8444-555555555555"
    workload = Workload(
        store.engine.url.render_as_string(hide_password=False), tmp_path / "records"
    )
    try:
        run = workload.submit(scenario, staging_batch=batch)
        plan = {
            "environment": batch,
            "batch_id": batch,
            "runs": [{"id": run, "scenario": scenario}],
        }
        workload.start(1, seconds=20)
        if scenario == "unknown":
            workload.approve(run)
            workload.wait(lambda: workload.status(run, "waiting_reconciliation"))
            before = m.collect_snapshot(store.engine, plan, batch)
            assert failures(before) == ()
            assert any(
                a.status == "unknown" and a.consumed_at is not None
                for a in before.actions
            )
            workload.reconcile(run)
        workload.wait(lambda: workload.store.get_run(run)["status"] == "succeeded")
        after = m.collect_snapshot(store.engine, plan, batch)
        assert failures(after) == ()
        if scenario == "unknown":
            from rc_acceptance import transition_failures

            assert transition_failures(before, after) == set()
            assert any(a.reconciled_at is not None for a in after.actions)
        else:
            assert any(s.summary_identity for s in after.steps)
            assert any(s.consumer_identity for s in after.steps)
            assert len(after.attempts) >= 3
    finally:
        workload.close()
