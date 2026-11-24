"""Make the Data tab's evidence screen available before web traffic.

Revision ID: w2a3b4c5d6e7
Revises: v1f2a3b4c5d6
"""
from alembic import op


revision = "w2a3b4c5d6e7"
down_revision = "v1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute('''CREATE TABLE IF NOT EXISTS public."New_provider_resume_screen" (
        provider_id uuid PRIMARY KEY, flags jsonb NOT NULL,
        source_sha256 char(64), screened_at timestamptz NOT NULL DEFAULT now()
    )''')


def downgrade() -> None:
    # The screen is operational audit data also used by the cleanup worker.
    # A code rollback must not erase its accumulated review decisions.
    pass
