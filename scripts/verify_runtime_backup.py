"""Real pg_dump/pg_restore roundtrip, exclusively in a newly-created temp cluster.

No URL/host/database input, no external deletion. Dump contains only synthetic RC
fixtures and is deleted with the isolated cluster. This is not HA/PITR testing.
"""

import argparse
import hashlib
import json

from audit_durable_runtime import audit
from rc_support import ROOT, TemporaryPostgres, migrate, run
from rc_workload import Workload
from sqlalchemy import select, text

from agentflow.application.ports.observed_usage import ObservedUsagePolicy
from agentflow.application.ports.provider_admission import ProviderAdmissionPolicy
from agentflow.infrastructure.persistence.models import Base, StepRow
from agentflow.infrastructure.persistence.store import json_bytes


def inventory(store):
    """Complete persistent facts, compared in memory only; never printed/exported."""
    with store.engine.connect() as db:
        return {
            table.name: sorted(
                (dict(row) for row in db.execute(select(table)).mappings()),
                key=lambda row: json.dumps(row, sort_keys=True),
            )
            for table in Base.metadata.sorted_tables
        }


def restore_historical_checkpoints(store, filename):
    """Reviewed synthetic terminal facts, never upgraded by the current runtime."""
    facts = json.loads(
        (ROOT / "tests/fixtures" / filename).read_text(encoding="utf-8")
    )["tables"]
    runs = {r["id"] for r in facts["runs"] if r["status"] in {"succeeded", "failed"}}
    workflows = {r["workflow_id"] for r in facts["runs"] if r["id"] in runs}
    steps = {s["id"] for s in facts["steps"] if s["run_id"] in runs}
    sessions = {s["id"] for s in facts["agent_sessions"] if s["step_row_id"] in steps}
    with store.engine.begin() as db:
        for table in Base.metadata.sorted_tables:
            records = []
            for row in facts[table.name]:
                include = (
                    row["session_id"] in sessions
                    if "session_id" in row
                    else row["step_row_id"] in steps
                    if "step_row_id" in row
                    else row["run_id"] in runs
                    if "run_id" in row
                    else row["id"] in runs
                    if table.name == "runs"
                    else row["id"] in workflows
                    if table.name == "workflows"
                    else False
                )
                if include:
                    # Event primary keys are local sequence identities. Preserve
                    # the audit facts without colliding with the new RC workload.
                    records.append(
                        {
                            k: v
                            for k, v in row.items()
                            if table.name != "events" or k != "id"
                        }
                    )
            if records:
                db.execute(table.insert(), records)
    return runs


def stop_at_admission_deferral(source, run):
    # Keep the observed boundary stable while killing owned workers. A deadline
    # may expire during shutdown, but claim() cannot take this locked Run.
    with source.store.transaction() as db:
        source.store._locked_run(db, run)
        step = db.scalar(
            select(StepRow)
            .where(StepRow.run_id == run, StepRow.key == "agent")
            .with_for_update()
        )
        if step.status != "ready" or step.error != "ProviderAdmissionDeferred":
            return False
        source.stop()
        return True


def drill(cluster):
    source_url = cluster.database("agentflow_rc_source")
    target_url = cluster.database("agentflow_rc_restored")
    migrate(source_url)
    admission = ProviderAdmissionPolicy(
        provider_key="rc-backup",
        max_concurrent_requests=2,
        max_requests_per_window=1000,
        window_seconds=60,
        permit_ttl_seconds=10,
        policy_version="1",
    )
    usage_policy = ObservedUsagePolicy(
        max_observed_total_tokens=10_000,
        missing_usage_policy="continue_with_warning",
        policy_version="1",
    )
    source = Workload(
        source_url,
        cluster.root / "source-records",
        admission_policy=admission,
        observed_usage_policy=usage_policy,
    )
    target = None
    try:
        source.start(seconds=120)
        done = source.submit("normal")
        source.wait(lambda: source.status(done, "succeeded"))
        source.wait(lambda: source.store.get_run(done)["status"] == "succeeded")
        reviewed = source.submit("review")
        source.approve(reviewed)
        source.wait(lambda: source.status(reviewed, "succeeded"))
        resolved = source.submit("reconciled")
        source.approve(resolved)
        source.wait(lambda: source.status(resolved, "waiting_reconciliation"))
        unresolved = source.submit("unknown")
        source.approve(unresolved)
        source.wait(lambda: source.status(unresolved, "waiting_reconciliation"))
        pending = source.submit("review")
        source.wait(lambda: source.status(pending, "waiting_approval"))
        cancelled = source.submit("review")
        source.wait(lambda: source.status(cancelled, "waiting_approval"))
        source.store.cancel(cancelled)
        delayed = source.submit("retry", retry_seconds=60)
        source.wait(
            lambda: (
                s
                if (s := source.state(delayed)) and s.step["error"] == "ModelRetryable"
                else None
            )
        )
        unknown_model = source.submit("model_unknown")
        source.wait(lambda: source.status(unknown_model, "failed"))
        limited_model = source.submit("model_rate_limited")
        source.wait(lambda: source.status(limited_model, "exhausted"))
        admission_delayed = source.submit("final")
        source.wait(lambda: stop_at_admission_deferral(source, admission_delayed))
        source.reconcile(resolved)
        legacy_runs = restore_historical_checkpoints(
            source.store, "checkpoint_v1_8219b93.json"
        )
        legacy_runs |= restore_historical_checkpoints(
            source.store, "checkpoint_v2_918ae92.json"
        )
        before = source.check()
        facts = inventory(source.store)
        checkpoints = {r: source.state(r) for r in source.runs}
        assert checkpoints[done].agent.usage_summary().total_tokens.value == 20
        assert facts["agent_model_attempts"]
        assert all(
            a["context_projection"] is not None for a in facts["agent_model_attempts"]
        )
        assert all(
            a["context_projection"]["checkpoint"]["references"]
            for a in facts["agent_model_attempts"]
        )
        assert all(
            a["request_contract"] is not None for a in facts["agent_model_attempts"]
        )
        assert {
            a["context_projection"]["checkpoint"]["checkpoint_version"]
            for a in facts["agent_model_attempts"]
        } == {1, 2, 3}
        assert all(
            a.context_projection.egress is not None
            and a.request_contract.context_egress_digest is not None
            for checkpoint in checkpoints.values()
            for a in checkpoint.agent.model_attempts
        )
        legacy = {r: source.invariants(source.store, r) for r in legacy_runs}
        assert facts["provider_admissions"] and facts["provider_permits"]
        assert facts["provider_admissions"][0]["cooldown_until"] > 0
        held = checkpoints[unknown_model].agent.model_permits[0]
        assert held.status == "unknown" and held.released_at is None
        assert checkpoints[admission_delayed].agent.snapshot.budget.model_attempts == 0
        with source.store.engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        for artifact in facts["artifacts"]:
            assert (
                artifact["digest"]
                == hashlib.sha256(json_bytes(artifact["content"])).hexdigest()
            )
        source.store.engine.dispose()
        backup = cluster.root / "synthetic.dump"
        common = [
            "-h",
            "127.0.0.1",
            "-p",
            str(cluster.port),
            "-U",
            "agentflow",
            "--no-password",
        ]
        run(
            [
                str(cluster.binaries / "pg_dump"),
                *common,
                "-Fc",
                "-f",
                str(backup),
                "agentflow_rc_source",
            ],
            timeout=60,
        )
        run(
            [
                str(cluster.binaries / "pg_restore"),
                *common,
                "--no-owner",
                "--exit-on-error",
                "-d",
                "agentflow_rc_restored",
                str(backup),
            ],
            timeout=60,
        )
        target = Workload(
            target_url,
            cluster.root / "restored-records",
            admission_policy=admission,
            observed_usage_policy=usage_policy,
        )
        target.runs = list(source.runs)
        target.scenarios = dict(source.scenarios)
        assert inventory(target.store) == facts, "RestoreFactMismatch"
        assert {r: target.invariants(target.store, r) for r in legacy_runs} == legacy
        migrate(target_url)
        with target.store.engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
        assert not audit(target.store.engine)["severe"]
        for r, checkpoint in checkpoints.items():
            assert target.state(r) == checkpoint
        # Recovered workers must reuse resolved results and consumed authority.
        target.start(seconds=120)
        target.wait(lambda: target.status(resolved, "succeeded"))
        target.wait(lambda: target.store.get_run(resolved)["status"] == "succeeded")
        target.wait(
            lambda: target.store.get_run(admission_delayed)["status"] == "succeeded"
        )
        assert target.state(unknown_model).agent == checkpoints[unknown_model].agent
        assert target.state(limited_model).agent == checkpoints[limited_model].agent
        assert target.state(admission_delayed).agent.snapshot.budget.model_attempts == 1
        assert target.state(unresolved).agent == checkpoints[unresolved].agent
        assert target.state(cancelled).agent == checkpoints[cancelled].agent
        assert target.state(pending).agent == checkpoints[pending].agent
        retry = target.state(delayed)
        assert retry.step["available_at"] == checkpoints[delayed].step["available_at"]
        assert retry.agent.snapshot.budget.model_attempts == 1
        target.reconcile(unresolved)
        target.wait(lambda: target.store.get_run(unresolved)["status"] == "succeeded")
        target.stop()
        assert not any(e["kind"] == "dispatch" for e in target.events()), (
            "RestoredActionRedispatched"
        )
        target.start(seconds=90)
        target.approve(pending)
        target.wait(lambda: target.store.get_run(pending)["status"] == "succeeded")
        target.stop()
        dispatched = [e["action"] for e in target.events() if e["kind"] == "dispatch"]
        assert dispatched == [checkpoints[pending].agent.actions[0].action.id]
        for r in (done, reviewed, resolved, unresolved, pending):
            assert len(target.store.artifacts(r)) == 3
        assert target.store.get_run(cancelled)["status"] == "cancelled"
        # The saved delayed deadline is consumed naturally, never moved earlier.
        target.start(seconds=90)
        target.wait(
            lambda: target.store.get_run(delayed)["status"] == "succeeded", seconds=75
        )
        target.stop()
        assert target.state(delayed).agent.snapshot.budget.model_attempts == 2
        assert target.state(delayed).step["attempts"] == 1
        target.fenced_claims()
        target.check()
        assert {r: target.invariants(target.store, r) for r in legacy_runs} == legacy
        return {
            "status": "PASS",
            "alembic": "0008",
            "tables_compared": len(facts),
            "source": before,
            "restored_runs": len(target.runs) + len(legacy_runs),
            "legacy_checkpoint_runs": len(legacy_runs),
            "newly_approved_dispatches": 1,
            "replayed_tool_dispatches": 0,
            "admission_restored": True,
            "observed_usage_restored": True,
            "checkpoint_versions_restored": [1, 2, 3],
            "backup": "logical pg_dump/pg_restore; not HA/PITR",
        }
    finally:
        if target:
            target.close()
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", required=True)
    args = parser.parse_args()
    with TemporaryPostgres(args.bin) as cluster:
        print(json.dumps(drill(cluster)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
