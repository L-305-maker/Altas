"""Bounded three-process PostgreSQL soak; --worker is an explicit staging worker.

Default orchestrator creates only an isolated local cluster. Never accepts an
external URL. Worker mode takes AGENTFLOW_RC_DATABASE_URL / AGENTFLOW_RC_RECORDS
and must only be pointed at a dedicated staging database with RC fixture data.
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import time

from rc_support import TemporaryPostgres, migrate
from rc_workload import Workload, serve
from sqlalchemy.engine import make_url


def validate_limits(seconds, workload):
    if not 1 <= seconds <= 3600 or not 1 <= workload <= 1000:
        raise ValueError("RC duration/workload out of bounds")


def exercise(cluster, workload, *, seconds, count, seed):
    started = time.monotonic()
    workload.deadline = started + seconds + 120
    randomizer = random.Random(seed)
    completed = []
    workload.start(seconds=seconds + 120)
    # Each handshake is based on persisted state, not a random sleep schedule.
    reviewed = workload.submit("review")
    workload.wait(lambda: workload.status(reviewed, "waiting_approval"))
    workload.stop()
    workload.approve(reviewed)  # Approval survives all original processes exiting.
    workload.start(seconds=seconds + 120)
    workload.wait(lambda: workload.status(reviewed, "succeeded"))
    workload.wait(lambda: workload.store.get_run(reviewed)["status"] == "succeeded")
    isolated = workload.store.get_run(reviewed)
    completed.append(reviewed)
    summarized = workload.submit("summary")
    workload.wait(lambda: workload.store.get_run(summarized)["status"] == "succeeded")
    completed.append(summarized)

    retry = workload.submit("retry")
    delayed = workload.wait(
        lambda: (
            s
            if (s := workload.state(retry)) and s.step["error"] == "ModelRetryable"
            else None
        )
    )
    deadline = delayed.step["available_at"]
    workload.stop()
    workload.start(seconds=seconds + 120)
    while True:
        state = workload.state(retry)
        # Read the clock after the checkpoint, so crossing the deadline between
        # reads cannot misclassify a legal reservation as an early retry.
        with workload.store.transaction() as db:
            now = workload.store._now(db)
        if now >= deadline:
            break
        assert state.step["available_at"] == deadline
        assert state.agent.snapshot.budget.model_attempts == 1, "EarlyModelRetry"
        time.sleep(0.03)
    workload.wait(lambda: workload.status(retry, "succeeded"))
    final = workload.state(retry)
    assert final.agent.snapshot.budget.model_attempts == 2
    assert final.step["attempts"] == 1
    assert len(final.agent.model_calls) == 1
    completed.append(retry)

    for cancel in (False, True):
        crash = workload.submit("crash")
        workload.approve(crash)
        inflight = workload.wait(
            lambda crash=crash: (
                s
                if (s := workload.state(crash))
                and any(a.status == "inflight" for a in s.agent.snapshot.actions)
                else None
            )
        )
        action = inflight.agent.actions[0].action.id
        dispatched = workload.wait(
            lambda action=action: next(
                (
                    e
                    for e in workload.events()
                    if e["kind"] == "dispatch" and e["action"] == action
                ),
                None,
            )
        )
        owner = next(p for p in workload.children if p.rc_slot == dispatched["slot"])
        workload.kill(owner)
        if cancel:
            # Other independent processes poll/claim while cancellation is committed.
            workload.store.cancel(crash)
            workload.wait(lambda crash=crash: workload.status(crash, "cancelled"))
        else:
            unknown = workload.wait(
                lambda crash=crash: workload.status(crash, "waiting_reconciliation")
            )
            assert unknown.agent.actions[0].action.status == "unknown"
            assert unknown.agent.approvals[0].consumed_at is not None
            workload.start(count=1, seconds=seconds + 120)
            workload.reconcile(crash)
            workload.wait(lambda crash=crash: workload.status(crash, "succeeded"))
            completed.append(crash)
        workload.check()

    # Actual server stop severs established connections. Processes must stop or
    # retain safe persisted state; restart always uses a fresh engine/pool.
    outage = workload.submit("unknown")
    workload.approve(outage)
    # Waiting Agent does not imply a quiescent DAG: the independent peer can
    # still complete and legitimately change Run.status before server shutdown.
    workload.wait(
        lambda: any(
            step["id"] == "peer" and step["status"] == "succeeded"
            for step in workload.store.get_run(outage)["steps"]
        )
    )
    before = workload.wait(lambda: workload.status(outage, "waiting_reconciliation"))
    cluster.stop()
    for child in workload.children:
        if child.poll() is None:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                workload.kill(child)
    workload.stop()
    cluster.start()
    assert any(e["kind"] == "database_stopped" for e in workload.events()), (
        "OutageNotObserved"
    )
    workload.store.engine.dispose()
    after = workload.state(outage)
    assert after == before, {
        "changed_fields": [
            field
            for field in ("agent", "step", "run_status", "continuation", "artifacts")
            if after is None or getattr(after, field) != getattr(before, field)
        ]
    }
    workload.start(seconds=seconds + 120)
    workload.reconcile(outage)
    workload.wait(lambda: workload.status(outage, "succeeded"))
    completed.append(outage)

    # Keep exercising all three independent workers for the requested duration.
    # count is maximum active wave size, not a hidden unbounded submission loop.
    waves = 0
    while time.monotonic() - started < seconds or waves == 0:
        scenarios = ["final", "normal", "review", "unknown", "retry", "cancel"]
        randomizer.shuffle(scenarios)
        batch = [
            (
                workload.submit(scenarios[i % len(scenarios)]),
                scenarios[i % len(scenarios)],
            )
            for i in range(count)
        ]
        for run, scenario in batch:
            if scenario in {"review", "unknown"}:
                workload.approve(run)
                if scenario == "unknown":
                    workload.reconcile(run)
            elif scenario == "cancel":
                workload.store.cancel(run)
        for run, scenario in batch:
            if scenario != "cancel":
                workload.wait(lambda run=run: workload.status(run, "succeeded"))
                completed.append(run)
        workload.check()
        waves += 1
    for run in completed:
        workload.wait(
            lambda run=run: workload.store.get_run(run)["status"] == "succeeded"
        )
        assert len(workload.store.artifacts(run)) == 3
    workload.stop()
    workload.fenced_claims()
    assert workload.store.get_run(reviewed) == isolated, "CrossRunMutation"
    summary = workload.check()
    assert len({e["pid"] for e in workload.events() if e["kind"] == "claim"}) >= 3
    return {
        "status": "PASS",
        "seed": seed,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "waves": waves,
        **summary,
        "cross_host": "NOT VERIFIED",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--workload", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    validate_limits(args.seconds, args.workload)
    if args.worker:
        url = os.environ["AGENTFLOW_RC_DATABASE_URL"]
        parsed = make_url(url)
        if parsed.get_backend_name() != "postgresql" or not (
            parsed.database or ""
        ).startswith("agentflow_rc_"):
            parser.error(
                "worker mode requires a dedicated agentflow_rc_ PostgreSQL database"
            )
        asyncio.run(
            serve(
                url,
                os.environ["AGENTFLOW_RC_RECORDS"],
                args.seconds,
            )
        )
        return 0
    if not args.bin:
        parser.error("--bin is required for an isolated soak")
    with TemporaryPostgres(args.bin) as cluster:
        url = cluster.database("agentflow_rc_source")
        migrate(url)
        workload = Workload(url, cluster.root / "records")
        try:
            print(
                json.dumps(
                    exercise(
                        cluster,
                        workload,
                        seconds=args.seconds,
                        count=args.workload,
                        seed=args.seed,
                    )
                )
            )
        finally:
            workload.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
