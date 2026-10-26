"""Constant-size aggregate snapshot. Query deadlines bound work, not a second queue."""

import time

from sqlalchemy import and_, case, func, or_, select, text

from agentflow.infrastructure.persistence.models import (
    AgentSessionRow,
    EventRow,
    RunRow,
    StepRow,
)

TERMINAL = ("succeeded", "failed", "cancelled")


def queue_snapshot(engine, *, timeout_seconds=5):
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("invalid queue diagnostic timeout")
    with engine.connect() as db:
        sqlite_connection = None
        try:
            if engine.dialect.name == "postgresql":
                db.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                db.execute(
                    text("SELECT set_config('statement_timeout', :value, true)"),
                    {"value": str(timeout_seconds * 1000)},
                )
                db.execute(text("SET LOCAL lock_timeout = '2s'"))
                now = float(
                    db.scalar(text("SELECT extract(epoch FROM clock_timestamp())"))
                )
            elif engine.dialect.name == "sqlite":
                db.execute(text("PRAGMA query_only=ON"))
                db.execute(text("BEGIN"))
                sqlite_connection = db.connection.driver_connection
                deadline = time.monotonic() + timeout_seconds
                sqlite_connection.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline), 1000
                )
                now = float(
                    db.scalar(select((func.julianday("now") - 2440587.5) * 86400))
                )
            else:
                raise ValueError("unsupported queue diagnostic database")
            ready = and_(StepRow.status == "ready", StepRow.available_at <= now)
            event_times = (
                select(
                    StepRow.id.label("step_id"),
                    func.max(EventRow.created_at).label("ready_at"),
                )
                .join(
                    EventRow,
                    and_(
                        EventRow.run_id == StepRow.run_id,
                        EventRow.data["step"].as_string() == StepRow.key,
                    ),
                )
                .where(
                    StepRow.status == "ready",
                    EventRow.kind.in_(("step.ready", "approval.approved")),
                )
                .group_by(StepRow.id)
                .subquery()
            )
            known_since = func.coalesce(event_times.c.ready_at, RunRow.created_at)
            since = case(
                (StepRow.available_at > known_since, StepRow.available_at),
                else_=known_since,
            )
            agent_status = AgentSessionRow.snapshot["status"].as_string()
            fields = {
                "ready_steps": ready,
                "delayed_steps": and_(
                    StepRow.status == "ready", StepRow.available_at > now
                ),
                "leased_steps": StepRow.status == "running",
                "expired_leases": and_(
                    StepRow.status == "running", StepRow.lease_until <= now
                ),
                "waiting_approval": and_(
                    StepRow.status == "waiting",
                    or_(
                        AgentSessionRow.id.is_(None), agent_status == "waiting_approval"
                    ),
                ),
                "waiting_reconciliation": and_(
                    StepRow.status == "waiting",
                    agent_status == "waiting_reconciliation",
                ),
                "ready_age_fallback_steps": and_(
                    ready, event_times.c.ready_at.is_(None), StepRow.available_at <= 0
                ),
            }
            query = (
                select(
                    *(
                        func.count().filter(condition).label(name)
                        for name, condition in fields.items()
                    ),
                    func.min(since).filter(ready).label("oldest_ready_at"),
                    select(func.count())
                    .select_from(RunRow)
                    .where(RunRow.status.not_in(TERMINAL))
                    .scalar_subquery()
                    .label("active_runs"),
                )
                .select_from(StepRow)
                .join(RunRow, RunRow.id == StepRow.run_id)
                .outerjoin(
                    AgentSessionRow,
                    AgentSessionRow.step_row_id == StepRow.id,
                )
                .outerjoin(event_times, event_times.c.step_id == StepRow.id)
                .where(RunRow.status.not_in(TERMINAL))
            )
            result = dict(db.execute(query).mappings().one())
            oldest = result.pop("oldest_ready_at")
            result["oldest_ready_wait_seconds"] = (
                max(0, now - oldest) if oldest is not None else None
            )
            return {
                "version": 1,
                "scope": {
                    "kind": "database_snapshot",
                    "as_of": now,
                    "timeout_seconds": timeout_seconds,
                    "pagination": None,
                },
                **result,
            }
        finally:
            if sqlite_connection is not None:
                sqlite_connection.set_progress_handler(None, 0)
            db.rollback()
            if engine.dialect.name == "sqlite":
                db.execute(text("PRAGMA query_only=OFF"))
