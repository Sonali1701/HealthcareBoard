"""add per-organization Quick Sourcer user limit

Revision ID: g6a7b8c9d0e1
Revises: f5a6b7c8d9e0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g6a7b8c9d0e1"
down_revision: Union[str, None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "employers",
        sa.Column("quick_sourcer_per_user_limit", sa.Integer(), nullable=False, server_default="10"),
    )


def downgrade() -> None:
    op.drop_column("employers", "quick_sourcer_per_user_limit")
