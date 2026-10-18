"""Attach reproducible context identity to new attempts; retain unknown legacy views."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_model_attempts", sa.Column("context_projection", sa.JSON, nullable=True)
    )


def downgrade():
    op.drop_column("agent_model_attempts", "context_projection")
