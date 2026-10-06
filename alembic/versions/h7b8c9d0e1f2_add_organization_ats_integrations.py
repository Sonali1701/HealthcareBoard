"""add organization ATS integrations

Revision ID: h7b8c9d0e1f2
Revises: g6a7b8c9d0e1
"""
from alembic import op
import sqlalchemy as sa


revision = "h7b8c9d0e1f2"
down_revision = "g6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ats_organization_integrations",
        sa.Column("integration_id", sa.String(length=36), nullable=False),
        sa.Column("employer_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="connected", nullable=False),
        sa.Column("settings_encrypted", sa.Text(), nullable=False),
        sa.Column("connected_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employer_id"], ["employers.employer_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connected_by_user_id"], ["users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("integration_id"),
        sa.UniqueConstraint("employer_id", "provider", name="uq_ats_organization_provider"),
    )
    op.create_index(
        "ix_ats_organization_employer", "ats_organization_integrations", ["employer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ats_organization_employer", table_name="ats_organization_integrations")
    op.drop_table("ats_organization_integrations")
