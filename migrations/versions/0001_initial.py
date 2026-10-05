"""创建 V1 持久执行结构；迁移是固定快照，不随 Python ORM 定义漂移。"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("spec", sa.JSON, nullable=False),
        sa.UniqueConstraint("name", "version"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workflow_id", sa.String(36), sa.ForeignKey("workflows.id"), nullable=False
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("spec", sa.JSON, nullable=False),
        sa.Column("inputs", sa.JSON, nullable=False),
        sa.Column("submission_key", sa.String(128), unique=True),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_table(
        "steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("available_at", sa.Float, nullable=False),
        sa.Column("lease_until", sa.Float),
        sa.Column("token", sa.String(36)),
        sa.Column("output", sa.JSON),
        sa.Column("error", sa.String(160)),
        sa.UniqueConstraint("run_id", "key"),
    )
    op.create_index("ix_steps_run_id", "steps", ["run_id"])
    op.create_index("ix_steps_queue", "steps", ["status", "available_at"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "step_id",
            sa.String(36),
            sa.ForeignKey("steps.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(80)),
        sa.Column("reason", sa.String(1000)),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("data", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_events_run_id", "events", ["run_id"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "step_id",
            sa.String(36),
            sa.ForeignKey("steps.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])


def downgrade():
    for table in ["artifacts", "events", "approvals", "steps", "runs", "workflows"]:
        op.drop_table(table)
