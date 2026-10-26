"""Bounded, owned PostgreSQL measurements; no production capacity promise."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rc_support import ROOT, TemporaryPostgres
from sqlalchemy import create_engine, event, func, select, text

from agentflow.runtime import HandlerRegistry, StepSpec, Store, Worker, WorkflowSpec


def distribution(values):
    ordered = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(ordered) * 1000, 3),
        "p95_ms": round(
            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000, 3
        ),
        "max_ms": round(max(ordered) * 1000, 3),
    }


def benchmark(url, samples):
    from agentflow.infrastructure.persistence.models import (
        AgentSessionRow,
        Base,
        RunRow,
        StepRow,
    )

    url += "&application_name=workflow-capacity-bench"
    observer = create_engine(url)
    stop, lock_waits = threading.Event(), []

    def monitor():
        with observer.connect() as db:
            while not stop.wait(0.02):
                lock_waits.append(
                    db.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                            "AND application_name='workflow-capacity-bench' AND wait_event_type='Lock'"
                        )
                    )
                )
                db.commit()

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    cases = []
    store = Store(url)

    def reset():
        Base.metadata.drop_all(store.engine)
        store.create_schema()

    def spec(size, name="bench", approval=False):
        return WorkflowSpec(
            name=name,
            steps=tuple(
                StepSpec(
                    id=f"s{i}",
                    handler="echo",
                    requires_approval=approval,
                    retry_delay=0,
                )
                for i in range(size)
            ),
        )

    def timed(call, values):
        start = time.perf_counter()
        result = call()
        values.append(time.perf_counter() - start)
        return result

    try:
        for size in (3, 100, 500):
            for active in (1, 24, 96):
                reset()
                wid = store.register(spec(size))
                submit_times, claim_times, waits, oldest = [], [], [], []
                for _ in range(active):
                    timed(lambda wid=wid: store.submit(wid, {}), submit_times)
                captured = []

                def capture(
                    _conn, _cursor, statement, parameters, *_, captured=captured
                ):
                    if "FOR UPDATE SKIP LOCKED" in statement and "runs" in statement:
                        captured.append((statement, parameters))

                event.listen(store.engine, "before_cursor_execute", capture)
                try:
                    for _ in range(samples):
                        with store.engine.connect() as db:
                            now = float(
                                db.scalar(
                                    text("SELECT extract(epoch FROM clock_timestamp())")
                                )
                            )
                            created = db.scalar(
                                select(func.min(RunRow.created_at)).where(
                                    RunRow.status.not_in(
                                        ("succeeded", "failed", "cancelled")
                                    ),
                                    select(StepRow.id)
                                    .where(
                                        StepRow.run_id == RunRow.id,
                                        StepRow.status == "ready",
                                        StepRow.available_at <= now,
                                    )
                                    .exists(),
                                )
                            )
                        if created is not None:
                            oldest.append(max(0, now - created))
                        task = timed(lambda: store.claim(60), claim_times)
                        if task:
                            waits.append(
                                max(0, now - store.get_run(task.run_id)["created_at"])
                            )
                            store.complete(task, {})
                finally:
                    event.remove(store.engine, "before_cursor_execute", capture)
                with store.engine.connect() as db:
                    plan = db.exec_driver_sql(
                        "EXPLAIN (FORMAT JSON) " + captured[0][0], captured[0][1]
                    ).scalar()
                nodes = []

                def walk(node, nodes=nodes):
                    nodes.append(
                        {
                            k: node[k]
                            for k in ("Node Type", "Index Name", "Plan Rows")
                            if k in node
                        }
                    )
                    for child in node.get("Plans", []):
                        walk(child)

                walk(plan[0]["Plan"])
                diagnostic_times = []
                diagnostic = timed(store.queue_diagnostics, diagnostic_times)
                cases.append(
                    {
                        "steps_per_run": size,
                        "active_runs": active,
                        "submit": distribution(submit_times),
                        "claim": distribution(claim_times),
                        "claim_calls_per_second": round(
                            len(claim_times) / sum(claim_times), 3
                        ),
                        "root_ready_wait": distribution(waits),
                        "oldest_eligible_root_wait": distribution(oldest),
                        "claim_plan": nodes,
                        "diagnostics": distribution(diagnostic_times),
                        "remaining_ready_steps": diagnostic["ready_steps"],
                    }
                )

        # Current ordering: one old 100-root DAG, with a fresh short Run per claim.
        reset()
        large = store.submit(store.register(spec(100, "old-large")), {})
        short = store.register(spec(1, "short"))
        short_runs, delays = [], []
        for _ in range(100):
            short_runs.append(store.submit(short, {}))
            task = store.claim(60)
            assert task.run_id == large
            store.complete(task, {})
        first_short = store.claim(60)
        assert first_short.run_id == short_runs[0]
        delays.append(time.time() - store.get_run(first_short.run_id)["created_at"])
        store.complete(first_short, {})
        fairness = {
            "old_ready_steps_before_first_short": 100,
            "short_runs_submitted": 100,
            "first_short_wait_ms": round(delays[0] * 1000, 3),
            "ordering": "oldest eligible Run; not round-robin",
        }

        # Actual independent Engines; calls can legitimately return None under SKIP LOCKED.
        reset()
        wid = store.register(spec(1))
        for _ in range(32):
            store.submit(wid, {})

        def consume(_):
            peer, durations, runs = Store(url), [], []
            try:
                for _ in range(40):
                    task = timed(lambda: peer.claim(60), durations)
                    if task:
                        runs.append(task.run_id)
                        peer.complete(task, {})
                return durations, runs
            finally:
                peer.close()

        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(consume, range(4)))
        duration = time.perf_counter() - start
        runs = [r for _, rs in results for r in rs]
        assert len(runs) == len(set(runs)) == 32
        concurrent = {
            "independent_engines": 4,
            "runs_completed": 32,
            "wall_seconds": round(duration, 3),
            "completed_per_second": round(32 / duration, 3),
            "claim": distribution([t for ts, _ in results for t in ts]),
        }

        # Durable waiting populations are created by the real existing Runner/review protocol.
        from agentflow.runtime import (
            DurableAgentRunner,
            MockProvider,
            SQLAgentStore,
            Tool,
            ToolRuntime,
            ToolSideEffect,
        )

        reset()
        plain_wait = store.register(spec(1, "step-wait", approval=True))
        agent_wid = store.register(spec(1, "agent-wait"))

        async def waits():
            async def invoke(_args):
                raise TimeoutError

            for protected in (True, False):
                for _ in range(12):
                    store.submit(agent_wid, {})
                    task = store.claim(60)
                    tools = ToolRuntime()
                    tools.register(
                        Tool(
                            "hold",
                            "synthetic",
                            {"type": "object"},
                            invoke,
                            requires_approval=protected,
                            side_effect=ToolSideEffect.READ_ONLY,
                        )
                    )
                    provider = MockProvider(
                        [
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "hold",
                                        "type": "function",
                                        "function": {"name": "hold", "arguments": "{}"},
                                    }
                                ],
                            }
                        ]
                    )
                    await DurableAgentRunner(SQLAgentStore(store), provider, tools).run(
                        task, "synthetic", allowed={"hold"}
                    )

        asyncio.run(waits())
        for _ in range(48):
            store.submit(plain_wait, {})
        wid = store.register(spec(1, "mixed"))
        expired = store.submit(wid, {})
        previous = store.claim(0.1)
        delayed_wid = store.register(
            WorkflowSpec(
                name="delayed",
                steps=(StepSpec(id="retry", handler="echo", retry_delay=60),),
            )
        )
        delayed = store.submit(delayed_wid, {})
        other = store.claim(60)
        # Real failure / deferral; test-owned setup changes only retry parameters.
        store.fail(other, RuntimeError())
        cancelled = store.submit(wid, {})
        store.cancel(cancelled)
        time.sleep(0.15)
        recovered = store.claim(60)
        assert recovered.run_id == expired and recovered.token != previous.token
        assert not store.complete(previous, {})
        store.complete(recovered, {})
        mixed_times = []
        for _ in range(samples):
            task = timed(lambda: store.claim(60), mixed_times)
            if task:
                store.complete(task, {})
        mixed = {
            "step_approvals": 48,
            "agent_approvals": 12,
            "agent_reconciliations": 12,
            "expired_lease_recovered": True,
            "stale_write_rejected": True,
            "cancelled_runs": 1,
            "retry_run": bool(delayed),
            "claim": distribution(mixed_times),
        }
        with store.engine.connect() as db:
            status = AgentSessionRow.snapshot["status"].as_string()
            waits_by_state = dict(
                db.execute(select(status, func.count()).group_by(status)).all()
            )
        assert waits_by_state == {"waiting_approval": 12, "waiting_reconciliation": 12}

        peaks = []
        for concurrency in (1, 4, 8):
            reset()
            wid = store.register(spec(1))
            for _ in range(16):
                store.submit(wid, {})

            async def workers(concurrency=concurrency):
                active = peak = finished = 0
                stop_work = asyncio.Event()
                handlers = HandlerRegistry()

                async def echo(_task):
                    nonlocal active, peak, finished
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.03)
                    active -= 1
                    finished += 1
                    if finished == 16:
                        stop_work.set()
                    return {}

                handlers.register("echo", echo)
                async with asyncio.timeout(60):
                    await Worker(store, handlers, concurrency=concurrency).serve(
                        stop_work
                    )
                return peak

            peaks.append(
                {
                    "configured": concurrency,
                    "observed_peak_handlers": asyncio.run(workers()),
                }
            )
        from agentflow.runtime import WorkflowAdmissionPolicy, WorkflowCapacityExceeded

        reset()
        governed = Store(
            url, workflow_admission=WorkflowAdmissionPolicy(max_active_runs=24)
        )
        try:
            wid = governed.register(spec(3))
            admission_times, denial_times = [], []
            for index in range(24):
                timed(
                    lambda index=index: governed.submit(wid, {}, f"admission-{index}"),
                    admission_times,
                )
            for _ in range(samples):
                start = time.perf_counter()
                try:
                    governed.submit(wid, {})
                    raise AssertionError("CapacityNotEnforced")
                except WorkflowCapacityExceeded:
                    denial_times.append(time.perf_counter() - start)
            diag_times = []
            report = timed(governed.queue_diagnostics, diag_times)
            assert report["active_runs"] == 24
            admission = {
                "max_active_runs": 24,
                "submit": distribution(admission_times),
                "rejected": distribution(denial_times),
                "diagnostics": distribution(diag_times),
            }
        finally:
            governed.close()
        with observer.connect() as db:
            pg_version = db.scalar(text("SHOW server_version"))
            deadlocks = db.scalar(
                text(
                    "SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()"
                )
            )
        return {
            "status": "PASS",
            "postgres": pg_version,
            "cases": cases,
            "fairness": fairness,
            "independent_workers": concurrent,
            "mixed_waits": mixed,
            "worker_concurrency": peaks,
            "opt_in_admission": admission,
            "lock_observation": {
                "poll_interval_seconds": 0.02,
                "samples": len(lock_waits),
                "max_waiters": max(lock_waits, default=0),
                "deadlocks": deadlocks,
            },
            "transaction_failures": 0,
            "production_capacity": "NOT_VERIFIED",
        }
    finally:
        stop.set()
        thread.join(5)
        store.close()
        observer.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", required=True)
    parser.add_argument("--samples", type=int, default=5, choices=range(3, 21))
    args = parser.parse_args()
    try:
        with TemporaryPostgres(args.bin) as pg:
            report = benchmark(pg.database("agentflow_rc_source"), args.samples)
        report["environment"] = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        }
        report["source_sha"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        report["script_sha256"] = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        report["core_sha256"] = hashlib.sha256(
            (ROOT / "src/agentflow/infrastructure/persistence/store.py").read_bytes()
        ).hexdigest()
        print(json.dumps(report))
        return 0
    except Exception as error:
        trace = error.__traceback__
        frames = []
        while trace.tb_next:
            frames.append(
                {
                    "file": Path(trace.tb_frame.f_code.co_filename).name,
                    "line": trace.tb_lineno,
                }
            )
            trace = trace.tb_next
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error": "CapacityBenchmarkFailed",
                    "check_line": trace.tb_lineno,
                    "check_file": Path(trace.tb_frame.f_code.co_filename).name,
                    "error_type": type(error).__name__,
                    "frames": [
                        frame
                        for frame in frames
                        if frame["file"] == Path(__file__).name
                    ],
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
