"""add provider profile fields

Revision ID: b7a91c2d3e4f
Revises: e9e3e290bcba
Create Date: 2026-07-08 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "b7a91c2d3e4f"
down_revision: Union[str, None] = "e9e3e290bcba"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROVIDER_COLUMNS = {
    "phone": sa.Column("phone", sa.String(length=30), nullable=True),
    "email": sa.Column("email", sa.String(length=255), nullable=True),
    "provider_category": sa.Column("provider_category", sa.String(length=20), nullable=True),
    "american_board": sa.Column("american_board", sa.String(length=150), nullable=True),
}

PROVIDER_INDEXES = {
    "ix_profiles_provider_category": ["provider_category"],
    "ix_profiles_american_board": ["american_board"],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("profiles")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("profiles")}

    with op.batch_alter_table("profiles", schema=None) as batch_op:
        for name, column in PROVIDER_COLUMNS.items():
            if name not in existing_columns:
                batch_op.add_column(column)
        for name, columns in PROVIDER_INDEXES.items():
            if name not in existing_indexes:
                batch_op.create_index(name, columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("profiles")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("profiles")}

    with op.batch_alter_table("profiles", schema=None) as batch_op:
        for name in PROVIDER_INDEXES:
            if name in existing_indexes:
                batch_op.drop_index(name)
        for name in reversed(PROVIDER_COLUMNS):
            if name in existing_columns:
                batch_op.drop_column(name)
