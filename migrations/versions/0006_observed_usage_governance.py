"""Freeze trusted deployment policy for a Run; old usage JSON stays unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("usage_governance", sa.JSON, nullable=True))


def downgrade():
    op.drop_column("runs", "usage_governance")
