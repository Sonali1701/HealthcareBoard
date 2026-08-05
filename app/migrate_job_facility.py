"""Add facility / agency / req-code columns to job_postings (idempotent).

Run:  python -m app.migrate_job_facility
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    "ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS facility VARCHAR(200)",
    "ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS agency VARCHAR(150)",
    "ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS req_code VARCHAR(60)",
    "CREATE INDEX IF NOT EXISTS ix_job_postings_facility ON job_postings (facility)",
    "CREATE INDEX IF NOT EXISTS ix_job_postings_req_code ON job_postings (req_code)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='job_postings' AND column_name IN "
            "('facility','agency','req_code') ORDER BY column_name")).scalars().all()
        print("OK - columns present:", list(cols))
    finally:
        db.close()


if __name__ == "__main__":
    run()
