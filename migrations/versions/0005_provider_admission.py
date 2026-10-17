"""Durable deployment-scoped request admission; existing attempts remain intact."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_sessions", sa.Column("provider_key", sa.String(80)))
    op.create_table(
        "provider_admissions",
        sa.Column("provider_key", sa.String(80), primary_key=True),
        sa.Column("policy", sa.JSON, nullable=False),
        sa.Column("cooldown_until", sa.Float, nullable=False),
        sa.Column(
            "revision", sa.BigInteger, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_provider_admission_revision"),
    )
    op.create_table(
        "provider_permits",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "provider_key",
            sa.String(80),
            sa.ForeignKey("provider_admissions.provider_key"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("model_call_id", sa.String(128), nullable=False),
        sa.Column("attempt_index", sa.Integer, nullable=False),
        sa.Column("reservation_revision", sa.Integer, nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
        sa.Column("released_at", sa.Float),
        sa.ForeignKeyConstraint(
            ["session_id", "model_call_id", "attempt_index"],
            [
                "agent_model_attempts.session_id",
                "agent_model_attempts.model_call_id",
                "agent_model_attempts.attempt_index",
            ],
        ),
        sa.UniqueConstraint("session_id", "model_call_id", "attempt_index"),
        sa.CheckConstraint(
            "status IN ('active','released','unknown')",
            name="ck_provider_permit_status",
        ),
        sa.CheckConstraint(
            "attempt_index >= 1 AND reservation_revision >= 1",
            name="ck_provider_permit_counts",
        ),
    )
    op.create_index(
        "ix_provider_permit_window", "provider_permits", ["provider_key", "created_at"]
    )
    op.create_index(
        "ix_provider_permit_active",
        "provider_permits",
        ["provider_key", "status", "expires_at"],
    )


def downgrade():
    op.drop_table("provider_permits")
    op.drop_table("provider_admissions")
    op.drop_column("agent_sessions", "provider_key")
