"""Add screening audit columns to profiles (idempotent).

Hiding a profile from the directory is a judgement call made by a heuristic, so
it has to be auditable and reversible: we record why it was hidden and what the
healthcare-signal score was, never just flip the bit.

Run:  python -m app.migrate_screen_fields
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS screen_reason VARCHAR(60)",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS screen_score SMALLINT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS screened_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_profiles_screen_reason ON profiles (screen_reason)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='profiles' AND column_name IN "
            "('screen_reason','screen_score','screened_at') ORDER BY column_name")
        ).scalars().all()
        print("OK - columns present:", list(cols))
    finally:
        db.close()


if __name__ == "__main__":
    run()
