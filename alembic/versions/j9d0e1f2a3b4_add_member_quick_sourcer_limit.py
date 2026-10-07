"""add per-member Quick Sourcer limit overrides

Revision ID: j9d0e1f2a3b4
Revises: i8c9d0e1f2a3
"""
from alembic import op
import sqlalchemy as sa


revision = "j9d0e1f2a3b4"
down_revision = "i8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employer_members",
        sa.Column("quick_sourcer_limit_override", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("employer_members", "quick_sourcer_limit_override")
