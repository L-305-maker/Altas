"""Bind immutable request identity to new attempts without inventing legacy facts."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_model_attempts", sa.Column("request_contract", sa.JSON, nullable=True)
    )


def downgrade():
    op.drop_column("agent_model_attempts", "request_contract")
