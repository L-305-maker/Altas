"""PostgreSQL 持久化引擎：状态、检查点、审批和事件在同一事务提交。

锁顺序固定为 run → step。PostgreSQL 通过 SKIP LOCKED 分配工作，SQLite
通过 BEGIN IMMEDIATE 串行化写事务，仅用于学习和单机测试。执行用户代码时
绝不持有数据库锁。交付语义是 at-least-once，token 只保证结果不被过期执行覆盖。
"""

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, create_engine, event, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from agentflow.domain.execution.task import Task
from agentflow.domain.workflow.spec import WorkflowSpec
from agentflow.infrastructure.persistence.models import (
    ApprovalRow,
    ArtifactRow,
    Base,
    EventRow,
    RunRow,
    StepRow,
    WorkflowRow,
)

TERMINAL = {"succeeded", "failed", "cancelled"}


class NotFoundError(Exception):
    """资源不存在；API 将其映射为 404。"""


class ConflictError(Exception):
    """同一幂等键对应不同请求，或终态资源收到不相容的操作。"""


def json_bytes(value: Any) -> bytes:
    """限制单个检查点大小，拒绝 NaN 和不可序列化的 Python 对象。"""
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False
    ).encode()
    if len(data) > 2_000_000:
        raise ValueError("JSON payload exceeds 2 MB")
    return data


class Store:
    def __init__(self, url: str):
        options: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(url, **options)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")

    def create_schema(self) -> None:
        """仅供测试/本地演示；生产环境使用 Alembic 的显式迁移。"""
        Base.metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            if self.engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            yield session

    def register(self, spec: WorkflowSpec) -> str:
        data = spec.model_dump(mode="json")
        json_bytes(data)
        try:
            with self.transaction() as session:
                existing = session.scalar(
                    select(WorkflowRow).where(
                        WorkflowRow.name == spec.name,
                        WorkflowRow.version == spec.version,
                    )
                )
                if existing:
                    if existing.spec != data:
                        raise ConflictError("workflow version is immutable")
                    return existing.id
                row = WorkflowRow(
                    id=str(uuid4()), name=spec.name, version=spec.version, spec=data
                )
                session.add(row)
                return row.id
        except IntegrityError:
            # 两个部署进程同时注册同一版本时，唯一约束决定胜者，再读已提交定义。
            return self.register(spec)

    def submit(self, workflow_id: str, inputs: dict, key: str | None = None) -> str:
        json_bytes(inputs)
        if key is not None and not 1 <= len(key) <= 128:
            raise ValueError("submission key must contain 1..128 characters")
        try:
            with self.transaction() as session:
                if key:
                    old = session.scalar(
                        select(RunRow).where(RunRow.submission_key == key)
                    )
                    if old:
                        if old.workflow_id != workflow_id or old.inputs != inputs:
                            raise ConflictError(
                                "submission key already used for a different request"
                            )
                        return old.id
                workflow = session.get(WorkflowRow, workflow_id)
                if workflow is None:
                    raise NotFoundError("workflow not found")
                run = RunRow(
                    id=str(uuid4()),
                    workflow_id=workflow_id,
                    status="running",
                    spec=workflow.spec,
                    inputs=inputs,
                    submission_key=key,
                    created_at=time.time(),
                )
                session.add(run)
                session.flush()
                for spec in WorkflowSpec.model_validate(run.spec).steps:
                    session.add(
                        StepRow(
                            id=str(uuid4()),
                            run_id=run.id,
                            key=spec.id,
                            status="pending",
                        )
                    )
                self._event(session, run.id, "run.created", {})
                session.flush()
                self._schedule(session, run)
                return run.id
        except IntegrityError:
            if key:
                with Session(self.engine) as session:
                    old = session.scalar(
                        select(RunRow).where(RunRow.submission_key == key)
                    )
                    if old and old.workflow_id == workflow_id and old.inputs == inputs:
                        return old.id
                raise ConflictError("submission key conflict") from None
            raise

    def _locked_run(self, session: Session, run_id: str) -> RunRow:
        run = session.scalar(
            select(RunRow).where(RunRow.id == run_id).with_for_update()
        )
        if run is None:
            raise NotFoundError("run not found")
        return run

    def _event(self, session: Session, run_id: str, kind: str, data: dict) -> None:
        # 事件只保存状态元数据，不写输入正文、凭据、模型提示词或异常消息。
        session.add(
            EventRow(run_id=run_id, kind=kind, data=data, created_at=time.time())
        )

    def _schedule(self, session: Session, run: RunRow) -> None:
        if run.status in TERMINAL:
            return
        steps = {
            row.key: row
            for row in session.scalars(select(StepRow).where(StepRow.run_id == run.id))
        }
        specs = WorkflowSpec.model_validate(run.spec).steps
        changed = True
        while changed:
            changed = False
            for spec in specs:
                row = steps[spec.id]
                if row.status != "pending":
                    continue
                parents = [steps[key].status for key in spec.depends_on]
                if any(status in {"failed", "cancelled"} for status in parents):
                    row.status = "cancelled"
                    self._event(session, run.id, "step.cancelled", {"step": row.key})
                    changed = True
                elif all(status == "succeeded" for status in parents):
                    row.status = "waiting" if spec.requires_approval else "ready"
                    if spec.requires_approval:
                        session.add(
                            ApprovalRow(
                                id=str(uuid4()),
                                run_id=run.id,
                                step_id=row.id,
                                decision="pending",
                                created_at=time.time(),
                            )
                        )
                    self._event(
                        session, run.id, f"step.{row.status}", {"step": row.key}
                    )
        statuses = {row.status for row in steps.values()}
        previous = run.status
        if statuses <= TERMINAL:
            run.status = "succeeded" if statuses == {"succeeded"} else "failed"
        elif "ready" in statuses or "running" in statuses:
            run.status = "running"
        else:
            run.status = "waiting"
        if run.status != previous:
            self._event(session, run.id, f"run.{run.status}", {})

    def claim(self, lease_seconds: float = 30) -> Task | None:
        """短事务领取一项工作，同时恢复该 run 中过期的执行租约。"""
        now = time.time()
        eligible = (
            select(StepRow.id)
            .where(
                StepRow.run_id == RunRow.id,
                or_(
                    and_(StepRow.status == "ready", StepRow.available_at <= now),
                    and_(StepRow.status == "running", StepRow.lease_until <= now),
                ),
            )
            .exists()
        )
        with self.transaction() as session:
            run = session.scalar(
                select(RunRow)
                .where(
                    RunRow.status.not_in(TERMINAL),
                    eligible,
                )
                .order_by(RunRow.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            specs = {s.id: s for s in WorkflowSpec.model_validate(run.spec).steps}
            rows = list(
                session.scalars(select(StepRow).where(StepRow.run_id == run.id))
            )
            for row in rows:
                if row.status == "running" and row.lease_until <= now:
                    self._fail(session, run, row, specs[row.key], "LeaseExpired", now)
            session.flush()
            self._schedule(session, run)
            for row in rows:
                if row.status == "ready" and row.available_at <= now:
                    row.status, row.token, row.lease_until = (
                        "running",
                        str(uuid4()),
                        now + lease_seconds,
                    )
                    row.attempts += 1
                    spec = specs[row.key]
                    self._event(
                        session,
                        run.id,
                        "step.running",
                        {"step": row.key, "attempt": row.attempts},
                    )
                    return Task(
                        run.id,
                        row.key,
                        row.token,
                        row.attempts,
                        spec,
                        run.inputs,
                        {r.key: r.output for r in rows if r.key in spec.depends_on},
                    )
            return None

    def _owned(self, session: Session, task: Task) -> tuple[RunRow, StepRow] | None:
        run = self._locked_run(session, task.run_id)
        row = session.scalar(
            select(StepRow).where(StepRow.run_id == run.id, StepRow.key == task.step_id)
        )
        if (
            run.status in TERMINAL
            or row is None
            or row.status != "running"
            or row.token != task.token
            or row.lease_until <= time.time()
        ):
            return None
        return run, row

    def heartbeat(self, task: Task, lease_seconds: float) -> bool:
        with self.transaction() as session:
            owned = self._owned(session, task)
            if not owned:
                return False
            owned[1].lease_until = time.time() + lease_seconds
            return True

    def complete(self, task: Task, output: Any) -> bool:
        encoded = json_bytes(output)
        with self.transaction() as session:
            owned = self._owned(session, task)
            if not owned:
                return False
            run, row = owned
            row.status, row.output, row.error = "succeeded", output, None
            row.token, row.lease_until = None, None
            if task.spec.artifact:
                session.add(
                    ArtifactRow(
                        id=str(uuid4()),
                        run_id=run.id,
                        step_id=row.id,
                        content=output,
                        digest=hashlib.sha256(encoded).hexdigest(),
                        created_at=time.time(),
                    )
                )
            self._event(session, run.id, "step.succeeded", {"step": row.key})
            session.flush()
            self._schedule(session, run)
            return True

    def _fail(self, session, run, row, spec, error: str, now: float):
        row.error, row.token, row.lease_until = error, None, None
        if row.attempts < spec.max_attempts:
            row.status = "ready"
            row.available_at = now + min(
                spec.retry_delay * 2 ** (row.attempts - 1), 3600
            )
            kind = "step.retrying"
        else:
            row.status, kind = "failed", "step.failed"
        self._event(
            session,
            run.id,
            kind,
            {"step": row.key, "error": error, "attempt": row.attempts},
        )

    def fail(self, task: Task, error: Exception) -> bool:
        with self.transaction() as session:
            owned = self._owned(session, task)
            if not owned:
                return False
            run, row = owned
            # 异常消息可能包含 HTTP Authorization 或输入片段，持久化仅保留类型。
            self._fail(
                session, run, row, task.spec, type(error).__name__[:160], time.time()
            )
            session.flush()
            self._schedule(session, run)
            return True

    def cancel(self, run_id: str) -> None:
        with self.transaction() as session:
            run = self._locked_run(session, run_id)
            if run.status in TERMINAL:
                return
            run.status = "cancelled"
            for row in session.scalars(select(StepRow).where(StepRow.run_id == run_id)):
                if row.status not in TERMINAL:
                    row.status, row.token, row.lease_until = "cancelled", None, None
            for row in session.scalars(
                select(ApprovalRow).where(ApprovalRow.run_id == run_id)
            ):
                if row.decision == "pending":
                    row.decision = "cancelled"
            self._event(session, run_id, "run.cancelled", {})

    def decide(
        self, approval_id: str, approve: bool, actor: str, reason: str = ""
    ) -> None:
        if not actor.strip() or len(actor) > 80 or len(reason) > 1000:
            raise ValueError("invalid approval actor or reason")
        with self.transaction() as session:
            approval = session.get(ApprovalRow, approval_id)
            if approval is None:
                raise NotFoundError("approval not found")
            run = self._locked_run(session, approval.run_id)
            session.refresh(approval)
            decision = "approved" if approve else "rejected"
            if approval.decision == decision:
                return
            if approval.decision != "pending" or run.status in TERMINAL:
                raise ConflictError("approval is no longer pending")
            approval.decision, approval.actor, approval.reason = decision, actor, reason
            row = session.get(StepRow, approval.step_id)
            row.status = "ready" if approve else "failed"
            row.error = None if approve else "ApprovalRejected"
            self._event(
                session,
                run.id,
                f"approval.{decision}",
                {"step": row.key, "actor": actor},
            )
            session.flush()
            self._schedule(session, run)

    def get_run(self, run_id: str) -> dict:
        with Session(self.engine) as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise NotFoundError("run not found")
            steps = session.scalars(
                select(StepRow).where(StepRow.run_id == run_id).order_by(StepRow.key)
            )
            return {
                "id": row.id,
                "workflow_id": row.workflow_id,
                "status": row.status,
                "created_at": row.created_at,
                "spec": row.spec,
                "steps": [
                    {
                        "id": s.key,
                        "status": s.status,
                        "attempts": s.attempts,
                        "output": s.output,
                        "error": s.error,
                    }
                    for s in steps
                ],
            }

    def list_runs(self, limit: int = 100) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {
                    "id": r.id,
                    "workflow_id": r.workflow_id,
                    "status": r.status,
                    "created_at": r.created_at,
                }
                for r in session.scalars(
                    select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
                )
            ]

    def list_workflows(self) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {"id": r.id, **r.spec}
                for r in session.scalars(
                    select(WorkflowRow).order_by(WorkflowRow.name, WorkflowRow.version)
                )
            ]

    def events(self, run_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {"id": r.id, "kind": r.kind, "data": r.data, "created_at": r.created_at}
                for r in session.scalars(
                    select(EventRow)
                    .where(EventRow.run_id == run_id, EventRow.id > after)
                    .order_by(EventRow.id)
                    .limit(limit)
                )
            ]

    def approvals(self, run_id: str) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {
                    "id": r.id,
                    "step_id": r.step_id,
                    "decision": r.decision,
                    "actor": r.actor,
                    "reason": r.reason,
                }
                for r in session.scalars(
                    select(ApprovalRow).where(ApprovalRow.run_id == run_id)
                )
            ]

    def artifacts(self, run_id: str) -> list[dict]:
        with Session(self.engine) as session:
            return [
                {
                    "id": r.id,
                    "digest": r.digest,
                    "content": r.content,
                    "created_at": r.created_at,
                }
                for r in session.scalars(
                    select(ArtifactRow).where(ArtifactRow.run_id == run_id)
                )
            ]

    def health(self) -> bool:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    def close(self) -> None:
        self.engine.dispose()
