import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from test_durable_agent_runner import store as store
from test_model_governance import submit, worker

from agentflow.application.ports.model_provider import ModelRetryableError
from agentflow.infrastructure.models.mock import MockProvider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version,missing_projection,missing_request",
    [("0006", 1, 1), ("0007", 0, 1), ("0008", 0, 0)],
)
async def test_read_only_audit_supports_old_schema_and_exposes_no_identity(
    store, monkeypatch, version, missing_projection, missing_request
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from audit_model_retry_upgrade import audit

    run = submit(store)
    assert await worker(store, MockProvider([ModelRetryableError()])).tick()
    config = Config("alembic.ini")
    monkeypatch.setenv(
        "AGENTFLOW_DATABASE_URL", store.engine.url.render_as_string(hide_password=False)
    )
    command.stamp(config, "head")
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    try:
        command.downgrade(config, version)
        event.listen(store.engine, "before_cursor_execute", observe)
        try:
            report = audit(store.engine)
        finally:
            event.remove(store.engine, "before_cursor_execute", observe)
        assert report["pending_retry_calls"] == 1
        assert report["missing_projection_manifests"] == missing_projection
        assert report["missing_request_contracts"] == missing_request
        assert report["model_inflight"] == 0
        assert report["retry_recovery_blocked"] == int(
            missing_projection or missing_request
        )
        assert run not in json.dumps(report)
        assert all(
            s not in {"UPDATE", "INSERT", "DELETE", "ALTER", "CREATE", "DROP"}
            for s in statements
        )
        assert (
            "ModelRequestContractUnavailable" in str(report)
            if missing_request
            else not report["findings"]
        )
        assert audit(store.engine, limit=1, offset=1)["scanned_sessions"] == 0
    finally:
        command.upgrade(config, "head")
        with store.engine.begin() as db:
            db.execute(text("DROP TABLE IF EXISTS alembic_version"))


def test_audit_reports_inflight_and_pages_without_identifiers(store, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from audit_model_retry_upgrade import audit
    from test_durable_model_request import reservation_with_contract
    from test_provider_admission import opened

    from agentflow.infrastructure.persistence.agent_store import SQLAgentStore

    agents = SQLAgentStore(store)
    tasks = []
    for _ in range(2):
        task, state = opened(agents)
        tasks.append(task)
        agents.commit_transition(task, reservation_with_contract(state))
    first = audit(store.engine, limit=1)
    assert first["scanned_sessions"] == first["model_inflight"] == 1
    assert first["scope"]["truncated"] and first["scope"]["next_offset"] == 1
    assert first["findings"] == [{"code": "ModelOutcomeUnknownOnRecovery", "count": 1}]
    assert not audit(store.engine, limit=1, offset=1)["scope"]["truncated"]
    assert all(
        t.run_id not in json.dumps(first) and t.token not in json.dumps(first)
        for t in tasks
    )
    with pytest.raises(ValueError):
        audit(store.engine, limit=10001)
