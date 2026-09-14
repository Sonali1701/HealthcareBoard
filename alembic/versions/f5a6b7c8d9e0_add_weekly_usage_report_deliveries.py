"""add weekly usage report delivery ledger

Revision ID: f5a6b7c8d9e0
Revises: d4e5f6a7b8c9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "weekly_usage_report_deliveries",
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=36), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint(
            "employer_id", "recipient_user_id", "period_start",
            name="uq_weekly_usage_report_delivery",
        ),
    )
    op.create_index(
        "ix_weekly_usage_report_deliveries_period_start",
        "weekly_usage_report_deliveries", ["period_start"],
    )
    op.create_index(
        "ix_weekly_usage_report_deliveries_status",
        "weekly_usage_report_deliveries", ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_weekly_usage_report_deliveries_status",
        table_name="weekly_usage_report_deliveries",
    )
    op.drop_index(
        "ix_weekly_usage_report_deliveries_period_start",
        table_name="weekly_usage_report_deliveries",
    )
    op.drop_table("weekly_usage_report_deliveries")
