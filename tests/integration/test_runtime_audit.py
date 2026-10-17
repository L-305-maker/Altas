import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import event, select
from test_action_review_runner import setup
from test_durable_agent_runner import store as store

from agentflow.application.orchestration.worker import Worker
from agentflow.domain.agent.review import ApprovalDecision
from agentflow.domain.workflow.spec import StepSpec, WorkflowSpec
from agentflow.infrastructure.persistence.models import AgentSessionRow, StepRow


def auditor():
    path = Path(__file__).resolve().parents[2] / "scripts/audit_durable_runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_audit", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audit_is_bounded_redacted_and_never_writes(store):
    run = store.submit(
        store.register(
            WorkflowSpec(name="PRIVATE", steps=(StepSpec(id="work", handler="plain"),))
        ),
        {"secret": "PRIVATE"},
    )
    module = auditor()
    store.submit(store.get_run(run)["workflow_id"], {})
    statements = []

    def observe(conn, cursor, statement, *args):
        statements.append(statement.upper())

    event.listen(store.engine, "before_cursor_execute", observe)
    try:
        report = module.audit(store.engine, limit=1, since=0, stale_seconds=1)
    finally:
        event.remove(store.engine, "before_cursor_execute", observe)
    assert report["steps"] == {"ready": 1}
    assert report["scope"]["truncated"] is True
    assert report["severe"] == []
    assert "PRIVATE" not in json.dumps(report) and run not in json.dumps(report)
    assert not any(
        s.startswith(("UPDATE", "INSERT", "DELETE", "BEGIN IMMEDIATE"))
        for s in statements
    )
    if store.engine.dialect.name == "postgresql":
        assert any("READ ONLY" in s for s in statements)
    with pytest.raises(ValueError):
        module.audit(store.engine, limit=10001)


def test_audit_reports_expired_lease_without_repairing_it(store):
    run = store.submit(
        store.register(
            WorkflowSpec(name="lease", steps=(StepSpec(id="work", handler="plain"),))
        ),
        {},
    )
    task = store.claim(30)
    with store.transaction() as db:
        row = db.scalar(select(StepRow).where(StepRow.run_id == run))
        row.lease_until = 0
    report = auditor().audit(store.engine)
    assert report["observations"]["expired_leases"] == 1
    assert report["severe"] == []
    with store.transaction() as db:
        row = db.scalar(select(StepRow).where(StepRow.run_id == run))
        assert row.token == task.token and row.status == "running"
        row.status = "PRIVATE"
    report = auditor().audit(store.engine)
    assert "PRIVATE" not in json.dumps(report)
    assert {"code": "invalid_step_status", "count": 1} in report["severe"]


@pytest.mark.asyncio
async def test_waiting_reviews_are_observations_but_inconsistent_revision_is_severe(
    store,
    monkeypatch,
    capsys,
):
    run, agents, _, _, _, handlers, _ = setup(store, unknown=True)
    await Worker(store, handlers).tick()
    module = auditor()
    report = module.audit(store.engine)
    assert report["waiting"]["waiting_approval"]["count"] == 1
    assert not report["severe"]
    approval = agents.list_pending_action_approvals()[0]
    agents.decide_action(
        approval.approval_id,
        ApprovalDecision(
            expected_revision=agents.review_history(approval.session_id)["revision"],
            decision="approved",
            actor="operator",
        ),
    )
    report = module.audit(store.engine)
    assert report["observations"]["approved_unconsumed"] == 1
    await Worker(store, handlers).tick()
    report = module.audit(store.engine)
    assert report["waiting"]["waiting_reconciliation"]["count"] == 1
    assert report["observations"]["unknown_actions"] == 1
    assert not report["severe"]
    store.cancel(run)
    assert not module.audit(store.engine)["severe"]
    with store.transaction() as db:
        db.scalar(select(AgentSessionRow)).revision += 1
    assert {"code": "revision_mismatch", "count": 1} in module.audit(store.engine)[
        "severe"
    ]
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(sys, "argv", ["audit_durable_runtime.py"])
    assert module.main() == 2
    assert "revision_mismatch" in capsys.readouterr().out
