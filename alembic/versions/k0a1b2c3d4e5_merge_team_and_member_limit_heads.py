"""merge enterprise team and member Quick Sourcer limit revisions

Revision ID: k0a1b2c3d4e5
Revises: c9d0e1f2a3b4, j9d0e1f2a3b4
"""
from typing import Sequence, Union


revision: str = "k0a1b2c3d4e5"
down_revision: Union[str, Sequence[str], None] = (
    "c9d0e1f2a3b4",
    "j9d0e1f2a3b4",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
