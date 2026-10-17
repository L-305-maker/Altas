"""Per-attempt usage facts; legacy call aggregates cannot prove individual usage."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    attempts = op.create_table(
        "agent_model_attempts",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("model_call_id", sa.String(128), primary_key=True),
        sa.Column("attempt_index", sa.Integer, primary_key=True),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("accounting", sa.String(24), nullable=False),
        sa.Column("model", sa.String(256)),
        sa.Column("request_id", sa.String(256)),
        sa.Column("raw_usage", sa.JSON),
        sa.Column("usage", sa.JSON, nullable=False),
        sa.Column("created_at", sa.Float),
        sa.Column("confirmed_at", sa.Float),
        sa.ForeignKeyConstraint(
            ["session_id", "model_call_id"],
            ["agent_model_calls.session_id", "agent_model_calls.model_call_id"],
        ),
        sa.CheckConstraint("attempt_index >= 1", name="ck_model_attempt_index"),
        sa.CheckConstraint(
            "outcome IN ('inflight','succeeded','failed','unknown','LEGACY_UNMEASURED')",
            name="ck_model_attempt_outcome",
        ),
        sa.CheckConstraint(
            "accounting IN ('recorded','LEGACY_UNMEASURED')",
            name="ck_model_attempt_accounting",
        ),
    )
    calls = sa.table(
        "agent_model_calls",
        sa.column("session_id"),
        sa.column("model_call_id"),
        sa.column("attempts"),
        sa.column("status"),
    )
    connection = op.get_bind()
    # Preserve aggregate counts without attributing the latest usage to an old
    # attempt. Older individual outcomes/timestamps were never recorded.
    for call in connection.execute(sa.select(calls)).mappings():
        for start in range(1, call["attempts"] + 1, 1000):
            connection.execute(
                attempts.insert(),
                [
                    {
                        "session_id": call["session_id"],
                        "model_call_id": call["model_call_id"],
                        "attempt_index": index,
                        "outcome": call["status"]
                        if index == call["attempts"]
                        else "LEGACY_UNMEASURED",
                        "accounting": "LEGACY_UNMEASURED",
                        "usage": {
                            "input_tokens": None,
                            "output_tokens": None,
                            "total_tokens": None,
                            "source": "unavailable",
                        },
                    }
                    for index in range(start, min(start + 1000, call["attempts"] + 1))
                ],
            )


def downgrade():
    # Explicit rollback drops only the new ledger. Legacy aggregates are retained.
    op.drop_table("agent_model_attempts")
