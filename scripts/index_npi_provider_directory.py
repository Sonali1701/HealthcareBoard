"""Build NPI provider search indexes online on the existing Neon directory DB."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.import_npi_provider_directory_20261112 import (  # noqa: E402
    DATABASE_NAME, ENV_FILE, direct_url, env_values,
)

INDEXES = (
    ("provider_directory_active_role_name_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_active_role_name_idx "
     "ON provider_directory (left(taxonomy_code, 4), lower(last_name), lower(first_name), npi) "
     "WHERE status = 'ACTIVE'"),
    ("provider_directory_active_credential_name_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_active_credential_name_idx "
     "ON provider_directory (upper(btrim(credential)), lower(last_name), lower(first_name), npi) "
     "WHERE status = 'ACTIVE'"),
    ("provider_directory_first_name_prefix_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_first_name_prefix_idx "
     "ON provider_directory (lower(first_name) text_pattern_ops)"),
    ("provider_directory_middle_name_prefix_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_middle_name_prefix_idx "
     "ON provider_directory (lower(middle_name) text_pattern_ops)"),
    ("provider_directory_last_name_prefix_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_last_name_prefix_idx "
     "ON provider_directory (lower(last_name) text_pattern_ops)"),
    ("provider_directory_city_prefix_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_city_prefix_idx "
     "ON provider_directory (lower(city) text_pattern_ops)"),
    ("provider_directory_state_specialty_name_idx",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS provider_directory_state_specialty_name_idx "
     "ON provider_directory (state_code, specialty, lower(last_name), lower(first_name), npi)"),
)


def main() -> int:
    url = direct_url(env_values(ENV_FILE)["DATABASE_URL"], DATABASE_NAME)
    with psycopg.connect(url, connect_timeout=20) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for name, ddl in INDEXES:
                started = time.monotonic()
                print(f"Building {name} concurrently...", flush=True)
                cur.execute(ddl)
                print(f"READY {name} in {time.monotonic() - started:.1f}s", flush=True)
            cur.execute("ANALYZE provider_directory")
            names = tuple(name for name, _ in INDEXES)
            placeholders = ", ".join(["%s"] * len(names))
            cur.execute(f"""
                SELECT indexname FROM pg_indexes
                WHERE schemaname='public' AND tablename='provider_directory'
                AND indexname IN ({placeholders})
                ORDER BY indexname
            """, names)
            print("VERIFIED=" + ",".join(row[0] for row in cur.fetchall()), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
