"""add provider filter indexes

Revision ID: d4e5f6a7b8c9
Revises: c3f4a5b6d7e8
Create Date: 2026-07-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import inspect


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3f4a5b6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROFILE_FILTER_INDEXES = {
    "ix_profiles_years_experience": ["years_experience"],
    "ix_profiles_email": ["email"],
    "ix_profiles_phone": ["phone"],
    "ix_profiles_provider_directory": [
        "provider_category",
        "profession_type",
        "state_code",
        "city",
        "years_experience",
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_indexes = {i["name"] for i in inspector.get_indexes("profiles")}

    with op.batch_alter_table("profiles", schema=None) as batch_op:
        for name, columns in PROFILE_FILTER_INDEXES.items():
            if name not in existing_indexes:
                batch_op.create_index(name, columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_indexes = {i["name"] for i in inspector.get_indexes("profiles")}

    with op.batch_alter_table("profiles", schema=None) as batch_op:
        for name in reversed(PROFILE_FILTER_INDEXES):
            if name in existing_indexes:
                batch_op.drop_index(name)
