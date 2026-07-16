"""Add profiles.is_listable and hide parser-junk profiles from the directory.

A profile is NOT listable when the parser produced a placeholder name like
'Provider' or 'Unknown Candidate' — those are hidden from the Providers tab.

    python -m app.migrate_listable            # add column + backfill
    python -m app.migrate_listable --report   # just show how many are junk

Safe to re-run. Also adds a partial index so the directory query
(WHERE is_listable AND provider_category = …) stays fast at millions of rows.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from .database import SessionLocal, engine
from .importers.parsing import is_real_name
from .models import Profile


def _add_column() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS "
                "is_listable BOOLEAN NOT NULL DEFAULT TRUE"))
        else:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
            if "is_listable" not in cols:
                conn.execute(text(
                    "ALTER TABLE profiles ADD COLUMN is_listable BOOLEAN NOT NULL DEFAULT 1"))
    print("Column is_listable ensured.")


def _add_index() -> None:
    if engine.dialect.name != "postgresql":
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_listable_cat "
                "ON profiles (provider_category, completion_score DESC, profile_id) "
                "WHERE is_listable"))
            print("Partial index ix_profiles_listable_cat ensured.")
        except Exception as e:  # noqa: BLE001
            print(f"Index step: {str(e)[:160]}")


def _backfill(report_only: bool = False) -> None:
    """Re-flag every profile with the structural junk-name detector.

    Streams (id, name, current flag) in batches so it scales to millions, and
    only writes the rows whose listable status actually changed.
    """
    db = SessionLocal()
    try:
        rows = db.execute(text(
            "SELECT profile_id, first_name, last_name, is_listable FROM profiles")).all()
        total = len(rows)
        changes = []          # (profile_id, new_listable)
        junk = 0
        for pid, first, last, listable in rows:
            good = is_real_name(first, last)
            if not good:
                junk += 1
            if bool(good) != bool(listable):
                changes.append({"pid": pid, "flag": good})
        print(f"{junk} of {total} profiles have junk names; {len(changes)} flag(s) to update.")
        if report_only:
            return
        for i in range(0, len(changes), 1000):
            db.execute(
                text("UPDATE profiles SET is_listable = :flag WHERE profile_id = :pid"),
                changes[i:i + 1000])
        db.commit()
        print(f"Done. {junk} hidden, {total - junk} listable.")
    finally:
        db.close()


def main() -> None:
    report = "--report" in sys.argv
    if report:
        _backfill(report_only=True)
        return
    _add_column()
    _backfill()
    _add_index()
    print("Done.")


if __name__ == "__main__":
    main()
