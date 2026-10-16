"""绑定行动的审批与独立对账审计；旧行动不自动获得版本或权限。"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_sessions",
        sa.Column(
            "continuation_pending",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column("agent_actions", sa.Column("tool_binding", sa.JSON, nullable=True))
    op.create_table(
        "agent_action_approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.id"),
            nullable=False,
        ),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("tool_version", sa.String(128), nullable=False),
        sa.Column("tool_policy_digest", sa.String(64), nullable=False),
        sa.Column("arguments_digest", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(80)),
        sa.Column("reason", sa.String(1000)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("decided_at", sa.Float),
        sa.Column("consumed_at", sa.Float),
        sa.ForeignKeyConstraint(
            ["session_id", "action_id"],
            ["agent_actions.session_id", "agent_actions.id"],
        ),
        sa.UniqueConstraint("session_id", "action_id"),
        sa.CheckConstraint(
            "decision IN ('pending','approved','rejected','cancelled')",
            name="ck_action_approval_decision",
        ),
    )
    op.create_index(
        "uq_agent_pending_approval",
        "agent_action_approvals",
        ["session_id"],
        unique=True,
        sqlite_where=sa.text("decision = 'pending'"),
        postgresql_where=sa.text("decision = 'pending'"),
    )
    op.create_table(
        "agent_action_reconciliations",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("agent_sessions.id"),
            nullable=False,
        ),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("request_revision", sa.Integer, nullable=False),
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("confirmed_status", sa.String(16), nullable=False),
        sa.Column("result", sa.JSON),
        sa.Column("error_code", sa.String(80)),
        sa.Column("evidence_reference", sa.String(1000), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("applied", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "action_id"],
            ["agent_actions.session_id", "agent_actions.id"],
        ),
        sa.CheckConstraint(
            "confirmed_status IN ('succeeded','failed')",
            name="ck_action_reconciliation_outcome",
        ),
        sa.CheckConstraint(
            "previous_status = 'unknown'", name="ck_action_reconciliation_source"
        ),
    )
    op.create_index(
        "uq_agent_applied_reconciliation",
        "agent_action_reconciliations",
        ["session_id", "action_id"],
        unique=True,
        sqlite_where=sa.text("applied"),
        postgresql_where=sa.text("applied"),
    )


def downgrade():
    op.drop_table("agent_action_reconciliations")
    op.drop_table("agent_action_approvals")
    op.drop_column("agent_actions", "tool_binding")
    op.drop_column("agent_sessions", "continuation_pending")
