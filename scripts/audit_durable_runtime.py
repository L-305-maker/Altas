"""Read-only, bounded database facts; no credentials or persisted content in output.

AGENTFLOW_DATABASE_URL is read from the environment, never printed. Prefer a
dedicated SELECT-only database role. Exit 2 means a severe consistency finding;
exit 1 means the audit itself could not finish (not an empty/healthy report).
"""

import argparse
import json
import math
import os
import time
from collections import Counter

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentflow.domain.agent.run import AgentSnapshot
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.persistence.models import (
    AgentActionApprovalRow,
    AgentActionRow,
    AgentSessionRow,
    ArtifactRow,
    RunRow,
    StepRow,
)


def audit(
    engine,
    *,
    limit=1000,
    since=0,
    stale_seconds=3600,
    after_step=None,
    timeout_seconds=30,
):
    if (
        type(limit) is not int
        or not 1 <= limit <= 10000
        or not math.isfinite(since)
        or since < 0
        or not math.isfinite(stale_seconds)
        or stale_seconds <= 0
        or not 1 <= timeout_seconds <= 120
    ):
        raise ValueError("invalid audit bounds")
    counts, observations, severe, waits = Counter(), Counter(), Counter(), {}
    deadline = time.monotonic() + timeout_seconds
    with engine.connect() as connection:
        try:
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                connection.execute(text("SET LOCAL statement_timeout = '5s'"))
                connection.execute(text("SET LOCAL lock_timeout = '2s'"))
                now = float(
                    connection.scalar(
                        select(func.extract("epoch", func.clock_timestamp()))
                    )
                )
            elif engine.dialect.name == "sqlite":
                connection.execute(text("PRAGMA query_only=ON"))
                connection.execute(text("BEGIN"))
                now = connection.scalar(
                    select((func.julianday("now") - 2440587.5) * 86400)
                )
            else:
                raise ValueError("unsupported audit database")
            with Session(bind=connection, autoflush=False) as db:
                query = (
                    select(StepRow, RunRow, AgentSessionRow)
                    .join(RunRow, StepRow.run_id == RunRow.id)
                    .outerjoin(
                        AgentSessionRow, AgentSessionRow.step_row_id == StepRow.id
                    )
                    .where(RunRow.created_at >= since)
                    .order_by(StepRow.id)
                    .limit(limit + 1)
                )
                if after_step:
                    query = query.where(StepRow.id > after_step)
                rows = db.execute(query).yield_per(1)
                scanned, truncated = 0, False
                for index, (step, run, session) in enumerate(rows):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("RuntimeAuditDeadline")
                    if index == limit:
                        truncated = True
                        break
                    scanned += 1
                    counts[step.status] += 1
                    if step.status == "running" and (step.lease_until or 0) <= now:
                        observations["expired_leases"] += 1
                    if run.status == "cancelled" and step.status not in {
                        "cancelled",
                        "succeeded",
                        "failed",
                    }:
                        severe["cancelled_run_active_step"] += 1
                    artifacts = db.scalar(
                        select(func.count())
                        .select_from(ArtifactRow)
                        .where(ArtifactRow.step_id == step.id)
                    )
                    try:
                        spec = next(
                            s
                            for s in WorkflowSpec.model_validate(run.spec).steps
                            if s.id == step.key
                        )
                        if artifacts != int(
                            step.status == "succeeded" and spec.artifact
                        ):
                            severe["artifact_count"] += 1
                    except (ValueError, StopIteration):
                        severe["invalid_workflow_spec"] += 1
                    if session is None:
                        continue
                    age = max(0, now - session.updated_at)
                    if age >= stale_seconds and step.status not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                    }:
                        observations["stalled_sessions"] += 1
                    try:
                        snapshot = AgentSnapshot.model_validate(session.snapshot)
                    except ValueError:
                        severe["invalid_snapshot"] += 1
                        continue
                    if snapshot.revision != session.revision:
                        severe["revision_mismatch"] += 1
                    status = snapshot.status.value
                    terminal = {
                        "succeeded": "succeeded",
                        "failed": "failed",
                        "exhausted": "failed",
                        "cancelled": "cancelled",
                    }
                    if (status in terminal and step.status != terminal[status]) or (
                        step.status in terminal and not snapshot.status.is_terminal
                    ):
                        severe["agent_step_terminal_mismatch"] += 1
                    if status in {"waiting_approval", "waiting_reconciliation"}:
                        entry = waits.setdefault(
                            status, {"count": 0, "oldest_seconds": 0}
                        )
                        entry["count"] += 1
                        entry["oldest_seconds"] = max(
                            entry["oldest_seconds"], round(age, 3)
                        )
                        if step.status != "waiting":
                            severe["agent_step_wait_mismatch"] += 1
                    elif step.status == "waiting":
                        severe["agent_step_wait_mismatch"] += 1
                    if step.status == "ready" and step.error == "ModelRetryable":
                        observations[
                            "due_model_retries"
                            if step.available_at <= now
                            else "future_model_retries"
                        ] += 1
                        if (
                            status != "ready_model"
                            or not session.continuation_pending
                            or step.token
                            or step.lease_until
                        ):
                            severe["invalid_deferred_retry"] += 1
                    for label, table, condition in (
                        (
                            "unknown_actions",
                            AgentActionRow,
                            AgentActionRow.status == "unknown",
                        ),
                        (
                            "approved_unconsumed",
                            AgentActionApprovalRow,
                            (AgentActionApprovalRow.decision == "approved")
                            & AgentActionApprovalRow.consumed_at.is_(None),
                        ),
                    ):
                        observations[label] += db.scalar(
                            select(func.count())
                            .select_from(table)
                            .where(table.session_id == session.id, condition)
                        )
                return {
                    "version": 1,
                    "scope": {
                        "since": since,
                        "limit": limit,
                        "scanned_steps": scanned,
                        "truncated": truncated,
                    },
                    "steps": dict(counts),
                    "waiting": waits,
                    "observations": dict(observations),
                    "severe": [
                        {"code": code, "count": count}
                        for code, count in sorted(severe.items())
                    ],
                }
        finally:
            connection.rollback()
            if engine.dialect.name == "sqlite":
                connection.execute(text("PRAGMA query_only=OFF"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--since", type=float, default=0, help="UTC Unix seconds, inclusive"
    )
    parser.add_argument("--stale-seconds", type=float, default=3600)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--after-step", help="exclusive StepRow UUID cursor; no IDs in report"
    )
    args = parser.parse_args()
    engine = None
    try:
        url = os.environ["AGENTFLOW_DATABASE_URL"]
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5}
            if url.startswith("postgresql")
            else {"timeout": 5},
        )
        report = audit(
            engine,
            limit=args.limit,
            since=args.since,
            stale_seconds=args.stale_seconds,
            after_step=args.after_step,
            timeout_seconds=args.timeout_seconds,
        )
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 2 if report["severe"] else 0
    except (KeyError, ValueError, TimeoutError, SQLAlchemyError):
        print(json.dumps({"error": "RuntimeAuditUnavailable"}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
