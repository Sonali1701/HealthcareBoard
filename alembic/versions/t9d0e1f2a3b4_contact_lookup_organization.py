"""Attribute website contact lookup jobs to an organization."""
from alembic import op
import sqlalchemy as sa


revision = "t9d0e1f2a3b4"
down_revision = "s8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("contact_lookup_jobs"):
        from app.models import ContactLookupJob
        ContactLookupJob.__table__.create(bind=bind, checkfirst=True)
        return
    if "employer_id" not in {column["name"] for column in inspector.get_columns("contact_lookup_jobs")}:
        op.add_column("contact_lookup_jobs", sa.Column(
            "employer_id", sa.String(36), sa.ForeignKey("employers.employer_id"), nullable=True))
    if "ix_contact_lookup_jobs_org_created" not in {
        index["name"] for index in inspector.get_indexes("contact_lookup_jobs")
    }:
        op.create_index("ix_contact_lookup_jobs_org_created", "contact_lookup_jobs",
                        ["employer_id", "created_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("contact_lookup_jobs"):
        return
    if "ix_contact_lookup_jobs_org_created" in {
        index["name"] for index in inspector.get_indexes("contact_lookup_jobs")
    }:
        op.drop_index("ix_contact_lookup_jobs_org_created", table_name="contact_lookup_jobs")
    if "employer_id" in {column["name"] for column in inspector.get_columns("contact_lookup_jobs")}:
        op.drop_column("contact_lookup_jobs", "employer_id")
