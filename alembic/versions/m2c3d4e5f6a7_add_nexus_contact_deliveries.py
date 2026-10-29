"""add durable Nexus contact deliveries

Revision ID: m2c3d4e5f6a7
Revises: l1b2c3d4e5f6
"""
from alembic import op
import sqlalchemy as sa


revision = "m2c3d4e5f6a7"
down_revision = "l1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nexus_contact_deliveries",
        sa.Column("delivery_id", sa.String(36), nullable=False),
        sa.Column("integration_id", sa.String(36), nullable=False),
        sa.Column("employer_id", sa.String(36), nullable=False),
        sa.Column("profile_id", sa.String(36), nullable=True),
        sa.Column("actor_user_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("nexus_candidate_id", sa.String(100), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["integration_id"], ["ats_organization_integrations.integration_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.profile_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("delivery_id"),
        sa.UniqueConstraint("integration_id", "profile_id", name="uq_nexus_delivery_profile"),
    )
    op.create_index("ix_nexus_delivery_ready", "nexus_contact_deliveries", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_nexus_delivery_ready", table_name="nexus_contact_deliveries")
    op.drop_table("nexus_contact_deliveries")
