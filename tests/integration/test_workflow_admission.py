"""Opt-in capacity uses persisted Run facts, not Worker or Provider permits."""

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from test_durable_agent_runner import store as store
from test_postgres import pgstore as pgstore

from agentflow.runtime import (
    ConflictError,
    Settings,
    StepSpec,
    Store,
    WorkflowSpec,
    build_handlers,
    create_app,
)


def policy(limit=1):
    from agentflow.runtime import WorkflowAdmissionPolicy

    return WorkflowAdmissionPolicy(max_active_runs=limit)


def configured(store, limit=1):
    return Store(
        store.engine.url.render_as_string(hide_password=False),
        workflow_admission=policy(limit),
    )


def workflow(store, *, approval=False):
    return store.register(
        WorkflowSpec(
            name="capacity",
            steps=(StepSpec(id="work", handler="echo", requires_approval=approval),),
        )
    )


def counts(store):
    from agentflow.infrastructure.persistence.models import (
        AgentModelAttemptRow,
        ApprovalRow,
        EventRow,
        ProviderPermitRow,
        RunRow,
        StepRow,
    )

    with store.transaction() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(table))
            for table in (
                RunRow,
                StepRow,
                EventRow,
                ApprovalRow,
                AgentModelAttemptRow,
                ProviderPermitRow,
            )
        )


def test_policy_is_explicit_strict_versioned_and_default_disabled():
    from agentflow.runtime import WorkflowAdmissionPolicy

    assert WorkflowAdmissionPolicy().max_active_runs is None
    assert Settings().workflow_admission.max_active_runs is None
    assert policy().policy_version == "v1"
    for invalid in (0, -1, True, "1", 1.5):
        with pytest.raises(ValidationError):
            policy(invalid)


def test_full_capacity_preserves_global_identity_and_rolls_back_all_rows(store):
    from agentflow.runtime import WorkflowCapacityExceeded

    governed = configured(store)
    try:
        wid = workflow(store, approval=True)
        run = governed.submit(wid, {"private": "PRIVATE"}, "global-key")
        before = counts(store)
        assert governed.submit(wid, {"private": "PRIVATE"}, "global-key") == run
        with pytest.raises(ConflictError):
            governed.submit(wid, {}, "global-key")
        for key in (None, "new-key"):
            with pytest.raises(
                WorkflowCapacityExceeded, match="WorkflowCapacityExceeded"
            ):
                governed.submit(wid, {}, key)
        assert counts(store) == before
        assert store.get_run(run)["status"] == "waiting"
        approval = store.approvals(run)[0]
        store.decide(approval["id"], True, "operator")
        task = governed.claim()
        governed.complete(task, {})
        assert governed.submit(wid, {}) != run
    finally:
        governed.close()


def test_independent_submitters_cannot_exceed_capacity_or_duplicate(store):
    from agentflow.runtime import WorkflowCapacityExceeded

    wid = workflow(store)

    def submit(index):
        peer = configured(store, 3)
        try:
            return peer.submit(wid, {}, f"key-{index}")
        except WorkflowCapacityExceeded:
            return None
        finally:
            peer.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted = [r for r in pool.map(submit, range(16)) if r]
    assert len(set(accepted)) == 3 and counts(store)[0] == 3
    for run in accepted:
        store.cancel(run)

    def repeat(_):
        peer = configured(store, 1)
        try:
            return peer.submit(wid, {}, "repeat")
        finally:
            peer.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert len(set(pool.map(repeat, range(16)))) == 1
    assert counts(store)[0] == 4


@pytest.mark.parametrize("lost_ack", [False, True])
def test_commit_rollback_and_lost_confirmation_preserve_capacity(store, lost_ack):
    from agentflow.runtime import WorkflowCapacityExceeded

    governed = configured(store)
    wid = workflow(store)

    def fault(*args):
        raise OperationalError("PRIVATE_SQL", None, None)

    event.listen(governed.engine, "commit", fault)
    if lost_ack:
        # DBAPI COMMIT occurred, but the caller did not receive confirmation.
        event.remove(governed.engine, "commit", fault)
        original = governed.transaction
        from contextlib import contextmanager

        @contextmanager
        def transaction():
            with original() as db:
                yield db
            fault()

        governed.transaction = transaction
    try:
        with pytest.raises(OperationalError):
            governed.submit(wid, {}, "identity")
    finally:
        if lost_ack:
            governed.transaction = original
        else:
            event.remove(governed.engine, "commit", fault)
    try:
        assert counts(store)[0] == int(lost_ack)
        run = governed.submit(wid, {}, "identity")
        assert governed.submit(wid, {}, "identity") == run
        with pytest.raises(WorkflowCapacityExceeded):
            governed.submit(wid, {})
        assert counts(store)[0] == 1
    finally:
        governed.close()


def test_api_capacity_429_conflict_409_database_503_and_no_retry_after(
    store, monkeypatch
):
    governed = configured(store)
    settings = Settings(
        provider="mock", api_token=SecretStr("test-operator-token-value")
    )
    app = create_app(settings, governed, build_handlers(settings, store=governed))
    try:
        wid = workflow(store)
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/api/runtime/queue").status_code == 401
            client.headers["Authorization"] = "Bearer test-operator-token-value"
            body = {"workflow_id": wid, "inputs": {}, "submission_key": "key"}
            assert client.post("/api/runs", json=body).status_code == 201
            assert client.post("/api/runs", json=body).status_code == 201
            denied = client.post("/api/runs", json={**body, "submission_key": "other"})
            assert denied.status_code == 429 and "retry-after" not in denied.headers
            assert denied.json() == {"detail": "WorkflowCapacityExceeded"}
            assert (
                client.post("/api/runs", json={**body, "inputs": {"x": 1}}).status_code
                == 409
            )
            assert client.get("/api/runtime/queue").status_code == 200

            def unavailable(*args):
                raise OperationalError("PRIVATE_SQL", None, None)

            monkeypatch.setattr(governed, "submit", unavailable)
            result = client.post("/api/runs", json=body)
            assert result.status_code == 503 and "PRIVATE" not in result.text
    finally:
        governed.close()


def process_submit(url, wid, key):
    from agentflow.runtime import WorkflowAdmissionPolicy, WorkflowCapacityExceeded

    peer = Store(url, workflow_admission=WorkflowAdmissionPolicy(max_active_runs=2))
    try:
        settings = Settings(
            provider="mock", api_token=SecretStr("test-process-operator-token")
        )
        with TestClient(
            create_app(settings, peer, build_handlers(settings, store=peer))
        ) as client:
            response = client.post(
                "/api/runs",
                headers={"Authorization": "Bearer test-process-operator-token"},
                json={"workflow_id": wid, "inputs": {}, "submission_key": key},
            )
            if response.status_code == 429:
                return None
            assert response.status_code == 201
            return response.json()["id"]
    except WorkflowCapacityExceeded:
        return None
    finally:
        peer.close()


@pytest.mark.skipif(
    not os.environ.get("AGENTFLOW_TEST_POSTGRES_URL"),
    reason="requires a dedicated PostgreSQL test database",
)
def test_postgres_independent_api_processes_share_the_atomic_limit(pgstore):
    wid = workflow(pgstore)
    url = pgstore.engine.url.render_as_string(hide_password=False)
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        values = [
            future.result(60)
            for future in [
                pool.submit(process_submit, url, wid, f"process-{i}") for i in range(12)
            ]
        ]
    assert len({value for value in values if value}) == 2
    assert counts(pgstore)[0] == 2


@pytest.mark.skipif(
    not os.environ.get("AGENTFLOW_TEST_POSTGRES_URL"),
    reason="requires a dedicated PostgreSQL test database",
)
def test_postgres_repeatable_read_admission_fails_closed(pgstore):
    from sqlalchemy.exc import InvalidRequestError

    governed = configured(pgstore)
    governed.engine = governed.engine.execution_options(
        isolation_level="REPEATABLE READ"
    )
    try:
        with pytest.raises(
            InvalidRequestError, match="WorkflowAdmissionIsolationUnsupported"
        ):
            governed.submit(workflow(pgstore), {})
        assert counts(pgstore)[0] == 0
    finally:
        governed.close()


def test_global_keys_cross_workflow_and_mixed_writers_are_explicit(store):
    from agentflow.runtime import WorkflowCapacityExceeded

    governed = configured(store)
    try:
        wid = workflow(store)
        first = governed.submit(wid, {}, "global")
        other = store.register(
            WorkflowSpec(name="other", steps=(StepSpec(id="x", handler="echo"),))
        )
        with pytest.raises(ConflictError):
            governed.submit(other, {}, "global")
        second = store.submit(
            other, {}
        )  # Historical uncontrolled writers remain compatible.
        assert counts(store)[0] == 2
        assert governed.submit(wid, {}, "global") == first
        with pytest.raises(WorkflowCapacityExceeded):
            governed.submit(wid, {})
        store.cancel(second)
        store.cancel(first)
        assert governed.submit(wid, {})
    finally:
        governed.close()


def test_rejection_metrics_can_disappear_without_changing_capacity(store, monkeypatch):
    from agentflow.infrastructure.telemetry.metrics import RuntimeMetrics
    from agentflow.runtime import WorkflowCapacityExceeded

    governed = configured(store)
    governed.metrics = RuntimeMetrics()
    wid = workflow(store)
    try:
        governed.submit(wid, {})
        with pytest.raises(WorkflowCapacityExceeded):
            governed.submit(wid, {})
        assert governed.queue_diagnostics()["admission_rejections_observed"] == 1

        def broken(*args):
            raise RuntimeError("PRIVATE")

        monkeypatch.setattr(governed.metrics, "record", broken)
        with pytest.raises(WorkflowCapacityExceeded):
            governed.submit(wid, {})
        governed.metrics = None
        assert governed.queue_diagnostics()["active_runs"] == 1
        assert governed.queue_diagnostics()["admission_rejections_observed"] is None
    finally:
        governed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_full_capacity_does_not_block_existing_agent_review_or_reconciliation(
    store, unknown
):
    from test_action_review_runner import approve_pending, setup

    from agentflow.infrastructure.persistence.models import AgentModelAttemptRow
    from agentflow.runtime import (
        ReconciliationDecision,
        Worker,
        WorkflowCapacityExceeded,
    )

    governed = configured(store)
    try:
        run, agents, _, provider, invoked, handlers, _ = setup(
            governed, protected=not unknown, unknown=unknown
        )
        worker = Worker(governed, handlers)
        assert await worker.tick()
        with store.transaction() as db:
            first_contract = db.scalar(
                select(AgentModelAttemptRow.request_contract).order_by(
                    AgentModelAttemptRow.created_at
                )
            )
        before = counts(store)
        with pytest.raises(WorkflowCapacityExceeded):
            governed.submit(store.get_run(run)["workflow_id"], {})
        assert counts(store) == before
        diagnostics = governed.queue_diagnostics()
        assert (
            diagnostics["waiting_reconciliation" if unknown else "waiting_approval"]
            == 1
        )
        assert diagnostics["active_runs"] == 1
        if unknown:
            assert not await worker.tick() and len(invoked) == 1
            action = agents.list_unknown_actions()[0]
            agents.reconcile_action(
                action["session_id"],
                action["action_id"],
                ReconciliationDecision(
                    request_id="capacity-review",
                    expected_revision=action["revision"],
                    confirmed_status="succeeded",
                    result={"value": 1},
                    evidence_reference="owned-mock-receipt",
                    actor="operator",
                ),
            )
        else:
            approve_pending(agents)
        assert await worker.tick()
        assert store.get_run(run)["status"] == "succeeded"
        assert len(invoked) == 1 and provider.calls == 2
        assert not await worker.tick()
        with store.transaction() as db:
            assert (
                db.scalar(
                    select(AgentModelAttemptRow.request_contract).order_by(
                        AgentModelAttemptRow.created_at
                    )
                )
                == first_contract
            )
        assert governed.submit(store.get_run(run)["workflow_id"], {})
    finally:
        governed.close()


def test_existing_delayed_retry_and_snapshot_continue_at_capacity(
    store, advance_database_clock
):
    from agentflow.runtime import WorkflowCapacityExceeded

    governed = configured(store)
    try:
        wid = workflow(store)
        run = governed.submit(wid, {})
        original = store.get_run(run)["spec"]
        task = governed.claim(300)
        governed.fail(task, RuntimeError())
        assert governed.queue_diagnostics()["delayed_steps"] == 1
        with pytest.raises(WorkflowCapacityExceeded):
            governed.submit(wid, {})
        advance_database_clock(2)
        retry = governed.claim(300)
        assert retry.run_id == run and retry.attempt == 2
        assert store.get_run(run)["spec"] == original
        governed.complete(retry, {})
        assert governed.submit(wid, {})
    finally:
        governed.close()


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_only_real_terminal_runs_release_capacity(store, terminal):
    governed = configured(store)
    try:
        wid = governed.register(
            WorkflowSpec(
                name="terminal",
                steps=(StepSpec(id="one", handler="echo", max_attempts=1),),
            )
        )
        run = governed.submit(wid, {})
        if terminal == "cancelled":
            governed.cancel(run)
        else:
            task = governed.claim(300)
            if terminal == "failed":
                governed.fail(task, RuntimeError())
            else:
                governed.complete(task, {})
        assert store.get_run(run)["status"] == terminal
        assert governed.queue_diagnostics()["active_runs"] == 0
        assert governed.submit(wid, {}) != run
    finally:
        governed.close()


def test_api_owned_store_uses_settings_but_injected_store_keeps_its_policy(store):
    settings = Settings(
        provider="mock",
        database_url=SecretStr(store.engine.url.render_as_string(hide_password=False)),
        api_token=SecretStr("test-settings-operator-token"),
        workflow_admission=policy(),
    )
    app = create_app(settings)
    try:
        assert app.state.store.workflow_admission == policy()
        wid = workflow(store)
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer test-settings-operator-token"
            assert (
                client.post(
                    "/api/runs", json={"workflow_id": wid, "inputs": {}}
                ).status_code
                == 201
            )
            assert (
                client.post(
                    "/api/runs", json={"workflow_id": wid, "inputs": {}}
                ).status_code
                == 429
            )
        assert (
            create_app(settings, store).state.store.workflow_admission.max_active_runs
            is None
        )
    finally:
        app.state.store.close()
