"""Attribute website contact lookup jobs to an organization."""
from alembic import op
import sqlalchemy as sa


revision = "t9d0e1f2a3b4"
down_revision = "s8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    from app.models import ContactLookupJob, ProviderDirectoryContactLookupJob
    for model, index_name in (
        (ContactLookupJob, "ix_contact_lookup_jobs_org_created"),
        (ProviderDirectoryContactLookupJob, "ix_directory_contact_lookup_org_created"),
    ):
        table_name = model.__tablename__
        inspector = sa.inspect(bind)
        if not inspector.has_table(table_name):
            model.__table__.create(bind=bind, checkfirst=True)
            continue
        if "employer_id" not in {column["name"] for column in inspector.get_columns(table_name)}:
            op.add_column(table_name, sa.Column("employer_id", sa.String(36), nullable=True))
        if index_name not in {index["name"] for index in inspector.get_indexes(table_name)}:
            op.create_index(index_name, table_name, ["employer_id", "created_at"])


def downgrade() -> None:
    for table_name, index_name in (
        ("contact_lookup_jobs", "ix_contact_lookup_jobs_org_created"),
        ("provider_directory_contact_lookup_jobs", "ix_directory_contact_lookup_org_created"),
    ):
        inspector = sa.inspect(op.get_bind())
        if not inspector.has_table(table_name):
            continue
        if index_name in {index["name"] for index in inspector.get_indexes(table_name)}:
            op.drop_index(index_name, table_name=table_name)
        if "employer_id" in {column["name"] for column in inspector.get_columns(table_name)}:
            op.drop_column(table_name, "employer_id")
