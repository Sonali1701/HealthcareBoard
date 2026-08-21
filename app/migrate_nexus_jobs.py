"""Add external-source provenance columns to job_postings (idempotent).

Run:  python -m app.migrate_nexus_jobs

New databases get these from create_all; this brings an existing Postgres/Neon
database up to date so the LaborEdge Nexus job sync can upsert against
(external_source, external_id).
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    "ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS external_source VARCHAR(30)",
    "ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS external_id VARCHAR(60)",
    "CREATE INDEX IF NOT EXISTS ix_job_postings_external_source ON job_postings (external_source)",
    "CREATE INDEX IF NOT EXISTS ix_job_postings_external_id ON job_postings (external_id)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        print("OK - job_postings.external_source / external_id added")
    finally:
        db.close()


if __name__ == "__main__":
    run()
