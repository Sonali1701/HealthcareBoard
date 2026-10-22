"""Index direct provider name, specialty, and job-category search.

The index is built concurrently because the production profiles table is large.
"""
from alembic import op
from sqlalchemy import text


revision = "l1b2c3d4e5f6"
down_revision = "k0a1b2c3d4e5"
branch_labels = None
depends_on = None


_INDEX = "ix_profiles_directory_search_trgm"
_EXPRESSION = (
    "lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '') || ' ' "
    "|| coalesce(specialty, '') || ' ' || coalesce(job_category, ''))"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        # A cancelled concurrent build leaves an invalid index behind. Remove
        # it before retrying so IF NOT EXISTS cannot hide that failure.
        invalid = op.get_bind().scalar(text("""
            SELECT NOT i.indisvalid FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = :name AND c.relnamespace = 'public'::regnamespace
        """), {"name": _INDEX})
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON profiles USING gin (({_EXPRESSION}) gin_trgm_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
