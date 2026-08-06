"""Add primary-source verification fields to licences (idempotent).

Run:  python -m app.migrate_license_verification
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    # verified_at / verification_source already exist; these record WHAT the
    # source said, so an expired or disciplined licence is visible rather than
    # just "unverified".
    "ALTER TABLE licenses ADD COLUMN IF NOT EXISTS verification_status VARCHAR(20)",
    "ALTER TABLE licenses ADD COLUMN IF NOT EXISTS verification_detail VARCHAR(300)",
    "ALTER TABLE licenses ADD COLUMN IF NOT EXISTS verified_by_user_id VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_licenses_verification_status ON licenses (verification_status)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        cols = db.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='licenses' "
            "AND column_name IN ('verification_status','verification_detail','verified_by_user_id') "
            "ORDER BY column_name")).scalars().all()
        print("OK - columns present:", list(cols))
    finally:
        db.close()


if __name__ == "__main__":
    run()
