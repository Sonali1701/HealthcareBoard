"""Add ordered Provider indexes and a maintained specialty search catalog."""
from alembic import op
from sqlalchemy import text


revision = "n3d4e5f6a7b8"
down_revision = "m2c3d4e5f6a7"
branch_labels = None
depends_on = None


INDEXES = {
    "ix_profiles_provider_browse_score": (
        "(completion_score DESC, profile_id) WHERE is_listable IS TRUE "
        "AND merged_into IS NULL AND job_category IS NOT NULL "
        "AND specialty IS NOT NULL"
    ),
    "ix_profiles_provider_category_score": (
        "(job_category, completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND specialty IS NOT NULL"
    ),
    "ix_profiles_provider_no_resume_score": (
        "(completion_score DESC, profile_id) WHERE is_listable IS TRUE "
        "AND merged_into IS NULL AND job_category IS NOT NULL "
        "AND specialty IS NOT NULL AND (resume_url IS NULL "
        "OR length(trim(resume_url)) = 0)"
    ),
    "ix_profiles_specialty_score": (
        "(specialty, completion_score DESC, profile_id) "
        "WHERE is_listable IS TRUE AND merged_into IS NULL "
        "AND job_category IS NOT NULL"
    ),
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("""
        CREATE TABLE IF NOT EXISTS provider_specialty_catalog_next (
          specialty_value text NOT NULL,
          specialty_key text NOT NULL,
          job_category text NOT NULL,
          PRIMARY KEY (job_category, specialty_value)
        )
    """)
    op.execute("""
        INSERT INTO provider_specialty_catalog_next
          (specialty_value, specialty_key, job_category)
        SELECT specialty, lower(trim(specialty)), job_category
          FROM profiles
         WHERE is_listable IS TRUE AND merged_into IS NULL
           AND specialty IS NOT NULL AND length(trim(specialty)) > 0
           AND job_category IS NOT NULL AND length(trim(job_category)) > 0
         GROUP BY specialty, lower(trim(specialty)), job_category
        ON CONFLICT (job_category, specialty_value) DO NOTHING
    """)
    kind = op.get_bind().scalar(text("""
        SELECT c.relkind FROM pg_class c
         WHERE c.oid=to_regclass('provider_specialty_catalog')
    """))
    if kind == "m":
        op.execute("DROP MATERIALIZED VIEW provider_specialty_catalog")
    elif kind == "r":
        op.execute("DROP TABLE provider_specialty_catalog")
    op.execute("ALTER TABLE provider_specialty_catalog_next RENAME TO provider_specialty_catalog")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_provider_specialty_catalog_search
        ON provider_specialty_catalog USING gin (specialty_key gin_trgm_ops)
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION update_provider_specialty_catalog()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.is_listable IS TRUE AND NEW.merged_into IS NULL
             AND nullif(trim(NEW.specialty),'') IS NOT NULL
             AND nullif(trim(NEW.job_category),'') IS NOT NULL THEN
            INSERT INTO provider_specialty_catalog
              (specialty_value, specialty_key, job_category)
            VALUES (NEW.specialty, lower(trim(NEW.specialty)), NEW.job_category)
            ON CONFLICT (job_category, specialty_value) DO NOTHING;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("DROP TRIGGER IF EXISTS trg_provider_specialty_catalog ON profiles")
    op.execute("""
        CREATE TRIGGER trg_provider_specialty_catalog
        AFTER INSERT OR UPDATE OF specialty, job_category, is_listable, merged_into
        ON profiles FOR EACH ROW EXECUTE FUNCTION update_provider_specialty_catalog()
    """)
    with op.get_context().autocommit_block():
        for name, definition in INDEXES.items():
            op.execute(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                f"ON profiles {definition}"
            )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_profiles_directory_search_gist")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_provider_specialty_catalog ON profiles")
    op.execute("DROP FUNCTION IF EXISTS update_provider_specialty_catalog()")
    op.execute("DROP TABLE IF EXISTS provider_specialty_catalog")
    with op.get_context().autocommit_block():
        for name in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
