"""Index profiles that still need a ZIP or city centroid in radius search."""
from alembic import op


revision = "q6a7b8c9d0e1"
down_revision = "p5f6a7b8c9d0"
branch_labels = None
depends_on = None


INDEXES = {
    "ix_profiles_missing_geo_zip_score": (
        "(substr(regexp_replace(zip_code, '[^0-9]', '', 'g'), 1, 5), "
        "completion_score DESC, profile_id) "
        "WHERE (lat IS NULL OR lng IS NULL) AND zip_code IS NOT NULL "
        "AND is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL"
    ),
    "ix_profiles_missing_geo_city_score": (
        "(lower(btrim(city)), upper(btrim(state_code)), "
        "completion_score DESC, profile_id) "
        "WHERE (lat IS NULL OR lng IS NULL) "
        "AND city IS NOT NULL AND state_code IS NOT NULL "
        "AND is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL AND specialty IS NOT NULL"
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
