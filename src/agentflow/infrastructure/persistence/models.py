"""SQL 表只表达持久化结构，调度规则集中在 Store 的事务里。

时间使用 UTC Unix 秒，便于跨数据库比较。JSON 用于定义和检查点，绝不序列化
Python 函数或异常实例。队列直接复用 steps 表，避免“状态成功但队列未提交”的双写。
"""

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
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
    usage_governance: Mapped[dict[str, Any] | None] = mapped_column(JSON)


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


class AgentSessionRow(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_agent_revision"),)
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    step_row_id: Mapped[str] = mapped_column(ForeignKey("steps.id"), unique=True)
    initial_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    continuation_pending: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    provider_key: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class AgentModelCallRow(Base):
    __tablename__ = "agent_model_calls"
    __table_args__ = (
        UniqueConstraint("session_id", "model_call_id"),
        UniqueConstraint("session_id", "round_index"),
        CheckConstraint(
            "round_index >= 1 AND attempts >= 1", name="ck_agent_model_counts"
        ),
        CheckConstraint(
            "status IN ('inflight', 'succeeded', 'failed', 'unknown')",
            name="ck_agent_model_status",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"))
    model_call_id: Mapped[str] = mapped_column(String(128))
    round_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class AgentModelAttemptRow(Base):
    __tablename__ = "agent_model_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "model_call_id"],
            ["agent_model_calls.session_id", "agent_model_calls.model_call_id"],
        ),
        CheckConstraint("attempt_index >= 1", name="ck_model_attempt_index"),
        CheckConstraint(
            "outcome IN ('inflight','succeeded','failed','unknown','LEGACY_UNMEASURED')",
            name="ck_model_attempt_outcome",
        ),
        CheckConstraint(
            "accounting IN ('recorded','LEGACY_UNMEASURED')",
            name="ck_model_attempt_accounting",
        ),
    )
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome: Mapped[str] = mapped_column(String(24))
    accounting: Mapped[str] = mapped_column(String(24))
    model: Mapped[str | None] = mapped_column(String(256))
    request_id: Mapped[str | None] = mapped_column(String(256))
    raw_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    usage: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float | None] = mapped_column(Float)
    confirmed_at: Mapped[float | None] = mapped_column(Float)
    context_projection: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ProviderAdmissionRow(Base):
    __tablename__ = "provider_admissions"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_provider_admission_revision"),
    )
    provider_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    cooldown_until: Mapped[float] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default=text("0")
    )
    created_at: Mapped[float] = mapped_column(Float)


class ProviderPermitRow(Base):
    __tablename__ = "provider_permits"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "model_call_id", "attempt_index"],
            [
                "agent_model_attempts.session_id",
                "agent_model_attempts.model_call_id",
                "agent_model_attempts.attempt_index",
            ],
        ),
        UniqueConstraint("session_id", "model_call_id", "attempt_index"),
        Index("ix_provider_permit_window", "provider_key", "created_at"),
        Index("ix_provider_permit_active", "provider_key", "status", "expires_at"),
        CheckConstraint(
            "status IN ('active','released','unknown')",
            name="ck_provider_permit_status",
        ),
        CheckConstraint(
            "attempt_index >= 1 AND reservation_revision >= 1",
            name="ck_provider_permit_counts",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider_key: Mapped[str] = mapped_column(
        ForeignKey("provider_admissions.provider_key")
    )
    session_id: Mapped[str] = mapped_column(String(128))
    model_call_id: Mapped[str] = mapped_column(String(128))
    attempt_index: Mapped[int] = mapped_column(Integer)
    reservation_revision: Mapped[int] = mapped_column(Integer)
    token_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    released_at: Mapped[float | None] = mapped_column(Float)


class AgentMessageRow(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence"),
        ForeignKeyConstraint(
            ["session_id", "model_call_id"],
            ["agent_model_calls.session_id", "agent_model_calls.model_call_id"],
        ),
        CheckConstraint("sequence >= 0", name="ck_agent_message_sequence"),
        CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_agent_message_role",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    model_call_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[float] = mapped_column(Float)


class AgentActionRow(Base):
    __tablename__ = "agent_actions"
    __table_args__ = (
        UniqueConstraint("session_id", "model_call_id", "call_index"),
        UniqueConstraint("session_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["session_id", "model_call_id"],
            ["agent_model_calls.session_id", "agent_model_calls.model_call_id"],
        ),
        CheckConstraint("call_index >= 0", name="ck_agent_action_index"),
        CheckConstraint(
            "status IN ('proposed', 'inflight', 'succeeded', 'failed', 'unknown', 'denied')",
            name="ck_agent_action_status",
        ),
    )
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_sessions.id"), primary_key=True
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    model_call_id: Mapped[str] = mapped_column(String(128))
    call_index: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(80))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    side_effect: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    result: Mapped[Any | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    tool_binding: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class AgentActionApprovalRow(Base):
    __tablename__ = "agent_action_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "action_id"],
            ["agent_actions.session_id", "agent_actions.id"],
        ),
        UniqueConstraint("session_id", "action_id"),
        Index(
            "uq_agent_pending_approval",
            "session_id",
            unique=True,
            sqlite_where=text("decision = 'pending'"),
            postgresql_where=text("decision = 'pending'"),
        ),
        CheckConstraint(
            "decision IN ('pending','approved','rejected','cancelled')",
            name="ck_action_approval_decision",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"))
    action_id: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(80))
    tool_version: Mapped[str] = mapped_column(String(128))
    tool_policy_digest: Mapped[str] = mapped_column(String(64))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[float] = mapped_column(Float)
    decided_at: Mapped[float | None] = mapped_column(Float)
    consumed_at: Mapped[float | None] = mapped_column(Float)


class AgentActionReconciliationRow(Base):
    __tablename__ = "agent_action_reconciliations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id", "action_id"],
            ["agent_actions.session_id", "agent_actions.id"],
        ),
        Index(
            "uq_agent_applied_reconciliation",
            "session_id",
            "action_id",
            unique=True,
            sqlite_where=text("applied"),
            postgresql_where=text("applied"),
        ),
        CheckConstraint(
            "confirmed_status IN ('succeeded','failed')",
            name="ck_action_reconciliation_outcome",
        ),
        CheckConstraint(
            "previous_status = 'unknown'", name="ck_action_reconciliation_source"
        ),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("agent_sessions.id"))
    action_id: Mapped[str] = mapped_column(String(128))
    request_revision: Mapped[int] = mapped_column(Integer)
    previous_status: Mapped[str] = mapped_column(String(16))
    confirmed_status: Mapped[str] = mapped_column(String(16))
    result: Mapped[Any | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    evidence_reference: Mapped[str] = mapped_column(String(1000))
    actor: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(String(1000))
    applied: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[float] = mapped_column(Float)
