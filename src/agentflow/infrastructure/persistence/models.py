"""SQL 表只表达持久化结构，调度规则集中在 Store 的事务里。

时间使用 UTC Unix 秒，便于跨数据库比较。JSON 用于定义和检查点，绝不序列化
Python 函数或异常实例。队列直接复用 steps 表，避免“状态成功但队列未提交”的双写。
"""

from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class WorkflowRow(Base):
    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    version: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"))
    status: Mapped[str] = mapped_column(String(16), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON)
    submission_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[float] = mapped_column(Float)


class StepRow(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("run_id", "key"),
        Index("ix_steps_queue", "status", "available_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=0)
    lease_until: Mapped[float | None] = mapped_column(Float)
    token: Mapped[str | None] = mapped_column(String(36))
    output: Mapped[Any | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(String(160))


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"), unique=True)
    decision: Mapped[str] = mapped_column(String(16), default="pending")
    actor: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[float] = mapped_column(Float)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    content: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
