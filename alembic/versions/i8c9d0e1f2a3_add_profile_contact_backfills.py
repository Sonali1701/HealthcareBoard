"""add profile contact backfill state

Revision ID: i8c9d0e1f2a3
Revises: h7b8c9d0e1f2
"""
from alembic import op
import sqlalchemy as sa

revision = "i8c9d0e1f2a3"
down_revision = "h7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profile_contact_backfills",
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("profiles.profile_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("medhunt_candidate_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(80), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("next_eligible_at", sa.DateTime(), nullable=True),
        sa.Column("enriched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_profile_contact_backfills_status", "profile_contact_backfills", ["status"])
    op.create_index("ix_profile_contact_backfills_run_id", "profile_contact_backfills", ["run_id"])
    op.create_index("ix_profile_contact_backfills_next_eligible_at", "profile_contact_backfills", ["next_eligible_at"])
    op.create_index("ix_profile_contact_backfills_enriched_at", "profile_contact_backfills", ["enriched_at"])


def downgrade() -> None:
    op.drop_table("profile_contact_backfills")
