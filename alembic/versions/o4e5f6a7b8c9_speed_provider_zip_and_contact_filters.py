"""Add ordered indexes for Provider ZIP and contact filters."""
from alembic import op


revision = "o4e5f6a7b8c9"
down_revision = "n3d4e5f6a7b8"
branch_labels = None
depends_on = None


INDEXES = {
    "ix_profiles_provider_zip_score": (
        "(substr(regexp_replace(zip_code, '[^0-9]', '', 'g'), 1, 5), "
        "completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL "
        "AND zip_code IS NOT NULL"
    ),
    "ix_profiles_provider_email_score": (
        "(completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL "
        "AND email IS NOT NULL AND length(trim(email)) > 0"
    ),
    "ix_profiles_provider_phone_score": (
        "(completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL "
        "AND phone IS NOT NULL AND length(trim(phone)) > 0"
    ),
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name, definition in INDEXES.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                       f"ON profiles {definition}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for name in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
