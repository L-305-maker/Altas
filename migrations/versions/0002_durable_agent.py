"""Durable Agent 会话、追加消息、模型预约及历史 Action。"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "step_row_id",
            sa.String(36),
            sa.ForeignKey("steps.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("initial_snapshot", sa.JSON, nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_agent_revision"),
    )
    op.create_table(
        "agent_model_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.id"),
            nullable=False,
        ),
        sa.Column("model_call_id", sa.String(128), nullable=False),
        sa.Column("round_index", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("usage", sa.JSON),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("session_id", "model_call_id"),
        sa.UniqueConstraint("session_id", "round_index"),
        sa.CheckConstraint(
            "round_index >= 1 AND attempts >= 1", name="ck_agent_model_counts"
        ),
        sa.CheckConstraint(
            "status IN ('inflight', 'succeeded', 'failed', 'unknown')",
            name="ck_agent_model_status",
        ),
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("model_call_id", sa.String(128)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("session_id", "sequence"),
        sa.ForeignKeyConstraint(
            ["session_id", "model_call_id"],
            ["agent_model_calls.session_id", "agent_model_calls.model_call_id"],
        ),
        sa.CheckConstraint("sequence >= 0", name="ck_agent_message_sequence"),
        sa.CheckConstraint(
            "role IN ('system', 'user', 'assistant', 'tool')",
            name="ck_agent_message_role",
        ),
    )
    op.create_table(
        "agent_actions",
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.id"),
            primary_key=True,
        ),
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("model_call_id", sa.String(128), nullable=False),
        sa.Column("call_index", sa.Integer, nullable=False),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("arguments", sa.JSON, nullable=False),
        sa.Column("side_effect", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("result", sa.JSON),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint("session_id", "model_call_id", "call_index"),
        sa.UniqueConstraint("session_id", "idempotency_key"),
        sa.ForeignKeyConstraint(
            ["session_id", "model_call_id"],
            ["agent_model_calls.session_id", "agent_model_calls.model_call_id"],
        ),
        sa.CheckConstraint("call_index >= 0", name="ck_agent_action_index"),
        sa.CheckConstraint(
            "status IN ('proposed', 'inflight', 'succeeded', 'failed', 'unknown', 'denied')",
            name="ck_agent_action_status",
        ),
    )


def downgrade():
    for table in [
        "agent_actions",
        "agent_messages",
        "agent_model_calls",
        "agent_sessions",
    ]:
        op.drop_table(table)
