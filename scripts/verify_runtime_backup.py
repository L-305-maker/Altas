"""Real pg_dump/pg_restore roundtrip, exclusively in a newly-created temp cluster.

No URL/host/database input, no external deletion. Dump contains only synthetic RC
fixtures and is deleted with the isolated cluster. This is not HA/PITR testing.
"""

import argparse
import hashlib
import json

from audit_durable_runtime import audit
from rc_support import TemporaryPostgres, migrate, run
from rc_workload import Workload
from sqlalchemy import select, text

from agentflow.infrastructure.persistence.models import Base
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


def drill(cluster):
    source_url = cluster.database("agentflow_rc_source")
    target_url = cluster.database("agentflow_rc_restored")
    migrate(source_url)
    source = Workload(source_url, cluster.root / "source-records")
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
        source.stop()  # Stop writers before taking the logical backup.
        source.reconcile(resolved)
        before = source.check()
        facts = inventory(source.store)
        checkpoints = {r: source.state(r) for r in source.runs}
        with source.store.engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
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
        target = Workload(target_url, cluster.root / "restored-records")
        target.runs = list(source.runs)
        target.scenarios = dict(source.scenarios)
        assert inventory(target.store) == facts, "RestoreFactMismatch"
        migrate(target_url)
        with target.store.engine.connect() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0003"
        assert not audit(target.store.engine)["severe"]
        for r, checkpoint in checkpoints.items():
            assert target.state(r) == checkpoint
        # Recovered workers must reuse resolved results and consumed authority.
        target.start(seconds=120)
        target.wait(lambda: target.status(resolved, "succeeded"))
        target.wait(lambda: target.store.get_run(resolved)["status"] == "succeeded")
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
        return {
            "status": "PASS",
            "alembic": "0003",
            "tables_compared": len(facts),
            "source": before,
            "restored_runs": len(target.runs),
            "newly_approved_dispatches": 1,
            "replayed_tool_dispatches": 0,
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
