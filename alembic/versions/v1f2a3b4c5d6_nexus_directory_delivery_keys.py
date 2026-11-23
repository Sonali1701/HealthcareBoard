"""Deduplicate Nexus deliveries from Provider and Data directory lookups.

Revision ID: v1f2a3b4c5d6
Revises: u0e1f2a3b4c5
"""
from alembic import op
import sqlalchemy as sa


revision = "v1f2a3b4c5d6"
down_revision = "u0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nexus_contact_deliveries", sa.Column("source_key", sa.String(80), nullable=True))
    op.create_unique_constraint("uq_nexus_delivery_source", "nexus_contact_deliveries", ["integration_id", "source_key"])


def downgrade() -> None:
    op.drop_constraint("uq_nexus_delivery_source", "nexus_contact_deliveries", type_="unique")
    op.drop_column("nexus_contact_deliveries", "source_key")
