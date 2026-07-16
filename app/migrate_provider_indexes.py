"""Create the indexes that keep the Providers directory fast at millions of rows.

    python -m app.migrate_provider_indexes

Safe to re-run. Uses CREATE INDEX CONCURRENTLY so building them does NOT lock
writes on a large table (it can take a while on 3-5M rows — that's expected).

What each index is for (all queries in GET /api/profiles):
  * ix_profiles_cat_score  — category browse + the ordered pagination
    (ORDER BY completion_score DESC, profile_id), so deep pages avoid a sort.
  * ix_profiles_search_trgm — trigram GIN so the name/keyword search
    (search_text LIKE '%q%') uses an index instead of scanning every row.
  * ix_profiles_city_lower — case-insensitive city prefix (lower(city) LIKE 'x%').
  * ix_profiles_experience / ix_profiles_board — the experience + board filters.
"""
from __future__ import annotations

from sqlalchemy import text

from .database import engine

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_cat_score "
    "ON profiles (provider_category, completion_score DESC, profile_id)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_search_trgm "
    "ON profiles USING gin (search_text gin_trgm_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_city_lower "
    "ON profiles (lower(city) text_pattern_ops)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_experience "
    "ON profiles (years_experience)",
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_board "
    "ON profiles (american_board)",
]


def main() -> None:
    if engine.dialect.name != "postgresql":
        print(f"Skipping: these indexes target PostgreSQL (dialect={engine.dialect.name}).")
        return
    # CONCURRENTLY cannot run inside a transaction — use an autocommit connection.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for sql in STATEMENTS:
            print(f"-> {sql[:88]}{'...' if len(sql) > 88 else ''}")
            try:
                conn.execute(text(sql))
                print("   ok")
            except Exception as e:  # noqa: BLE001 — report and continue
                print(f"   FAILED: {str(e)[:200]}")
    print("Done. Verify with:  \\di ix_profiles_*  (psql)")


if __name__ == "__main__":
    main()
