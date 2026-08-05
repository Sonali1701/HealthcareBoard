"""Add duplicate-merge tracking to profiles (idempotent).

A merge hides the losing rows rather than deleting them: the résumé, audit
history and any pool membership stay intact, and `merged_into` records which
profile absorbed them so the whole operation can be undone.

Run:  python -m app.migrate_merge_fields
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS merged_into VARCHAR(36)",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS merged_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_profiles_merged_into ON profiles (merged_into)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='profiles' "
            "AND column_name IN ('merged_into','merged_at') ORDER BY column_name")).scalars().all()
        print("OK - columns present:", list(cols))
    finally:
        db.close()


if __name__ == "__main__":
    run()
