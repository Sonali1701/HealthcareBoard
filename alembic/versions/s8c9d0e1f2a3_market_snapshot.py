"""Persist expensive market aggregates across web workers and deploys."""
from alembic import op
import sqlalchemy as sa


revision = "s8c9d0e1f2a3"
down_revision = "r7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics_market_snapshot",
        sa.Column("snapshot_id", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("analytics_market_snapshot")
