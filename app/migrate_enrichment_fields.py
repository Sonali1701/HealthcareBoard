"""Add profiles.work_authorization + profiles.education for résumé enrichment.

    python -m app.migrate_enrichment_fields            # add the columns
    python -m app.migrate_enrichment_fields --report   # coverage so far

Safe to re-run. Populate the columns with `python -m app.enrich_profiles`.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from .database import SessionLocal, engine


def _add_columns() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS "
                              "work_authorization VARCHAR(80)"))
            conn.execute(text("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS "
                              "education JSON"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_profiles_work_auth "
                              "ON profiles (work_authorization)"))
        else:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
            if "work_authorization" not in cols:
                conn.execute(text("ALTER TABLE profiles ADD COLUMN work_authorization VARCHAR(80)"))
            if "education" not in cols:
                conn.execute(text("ALTER TABLE profiles ADD COLUMN education JSON"))
    print("Columns work_authorization + education ensured.")


def _report() -> None:
    db = SessionLocal()
    try:
        tot, wa, ed, av = db.execute(text(
            "SELECT COUNT(*), COUNT(work_authorization), COUNT(education), "
            "COUNT(available_date) FROM profiles")).one()
        print(f"profiles                 : {tot:,}")
        print(f"  work_authorization set : {wa:,}")
        print(f"  education set           : {ed:,}")
        print(f"  available_date set      : {av:,}")
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
