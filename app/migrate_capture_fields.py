"""Add capture provenance / recruiter-ownership columns to profiles.

    python -m app.migrate_capture_fields            # add the columns
    python -m app.migrate_capture_fields --report   # coverage so far

Safe to re-run. Populated by the central ingest endpoint (POST /api/ingest/candidate).
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from .database import SessionLocal, engine


def _add_columns() -> None:
    dialect = engine.dialect.name
    cols = {
        "capture_source": "VARCHAR(60)",
        "captured_by_user_id": "VARCHAR(36)",
        "captured_by_email": "VARCHAR(255)",
        "captured_at": "TIMESTAMPTZ" if dialect == "postgresql" else "TIMESTAMP",
    }
    with engine.begin() as conn:
        if dialect == "postgresql":
            for name, typ in cols.items():
                conn.execute(text(f"ALTER TABLE profiles ADD COLUMN IF NOT EXISTS {name} {typ}"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_profiles_captured_by "
                              "ON profiles (captured_by_user_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_profiles_capture_source "
                              "ON profiles (capture_source)"))
        else:
            have = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
            for name, typ in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE profiles ADD COLUMN {name} {typ}"))
    print("Capture/ownership columns ensured.")


def _report() -> None:
    db = SessionLocal()
    try:
        tot, cap = db.execute(text(
            "SELECT COUNT(*), COUNT(captured_by_user_id) FROM profiles")).one()
        print(f"profiles            : {tot:,}")
        print(f"  captured (owned)  : {cap:,}")
        rows = db.execute(text(
            "SELECT capture_source, COUNT(*) c FROM profiles "
            "WHERE capture_source IS NOT NULL GROUP BY capture_source ORDER BY c DESC")).all()
        for src, c in rows:
            print(f"    {src:<24} {c:,}")
    finally:
        db.close()


def main() -> None:
    if "--report" in sys.argv:
        _report()
        return
    _add_columns()
    _report()
    print("Done.")


if __name__ == "__main__":
    main()
