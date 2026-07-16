"""add provider contact audit fields

Revision ID: c3f4a5b6d7e8
Revises: b7a91c2d3e4f
Create Date: 2026-07-08 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "c3f4a5b6d7e8"
down_revision: Union[str, None] = "b7a91c2d3e4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONTACT_AUDIT_COLUMNS = {
    "contact_updated_by_user_id": sa.Column("contact_updated_by_user_id", sa.String(length=36), nullable=True),
    "contact_updated_by_email": sa.Column("contact_updated_by_email", sa.String(length=255), nullable=True),
    "contact_updated_at": sa.Column("contact_updated_at", sa.DateTime(), nullable=True),
}

CONTACT_AUDIT_INDEXES = {
    "ix_profiles_contact_updated_by_user_id": ["contact_updated_by_user_id"],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("profiles")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("profiles")}

    with op.batch_alter_table("profiles", schema=None) as batch_op:
        for name, column in CONTACT_AUDIT_COLUMNS.items():
            if name not in existing_columns:
                batch_op.add_column(column)
        for name, columns in CONTACT_AUDIT_INDEXES.items():
            if name not in existing_indexes:
                batch_op.create_index(name, columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns("profiles")}
    existing_indexes = {i["name"] for i in inspector.get_indexes("profiles")}

    with op.batch_alter_table("profiles", schema=None) as batch_op:
        for name in CONTACT_AUDIT_INDEXES:
            if name in existing_indexes:
                batch_op.drop_index(name)
        for name in reversed(CONTACT_AUDIT_COLUMNS):
            if name in existing_columns:
                batch_op.drop_column(name)
