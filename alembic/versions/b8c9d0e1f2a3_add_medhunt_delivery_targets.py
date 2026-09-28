"""add per-user Medhunt delivery targets

Revision ID: b8c9d0e1f2a3
Revises: a6b7c8d9e0f1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("medhunt_ceipal_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "users", sa.Column("medhunt_nexus_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("users", "medhunt_nexus_enabled")
    op.drop_column("users", "medhunt_ceipal_enabled")
