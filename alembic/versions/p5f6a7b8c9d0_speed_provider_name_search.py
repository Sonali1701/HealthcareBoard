"""Add ordered name indexes for Provider directory search."""
from alembic import op


revision = "p5f6a7b8c9d0"
down_revision = "o4e5f6a7b8c9"
branch_labels = None
depends_on = None


INDEXES = {
    "ix_profiles_provider_first_name_score": (
        "(lower(trim(first_name)), completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL "
        "AND first_name IS NOT NULL"
    ),
    "ix_profiles_provider_last_name_score": (
        "(lower(trim(last_name)), completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL "
        "AND last_name IS NOT NULL"
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
