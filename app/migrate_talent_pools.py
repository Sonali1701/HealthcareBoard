"""Create the talent-pool tables (idempotent).

Run:  python -m app.migrate_talent_pools
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    """
    CREATE TABLE IF NOT EXISTS talent_pools (
        pool_id       VARCHAR(36) PRIMARY KEY,
        owner_user_id VARCHAR(36) NOT NULL REFERENCES users(user_id),
        name          VARCHAR(120) NOT NULL,
        description   TEXT,
        job_id        VARCHAR(36) REFERENCES job_postings(job_id) ON DELETE SET NULL,
        color         VARCHAR(16) DEFAULT 'blue',
        created_at    TIMESTAMP NOT NULL DEFAULT now(),
        updated_at    TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pool_owner_name ON talent_pools (owner_user_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_talent_pools_name ON talent_pools (name)",
    "CREATE INDEX IF NOT EXISTS ix_talent_pools_job_id ON talent_pools (job_id)",
    """
    CREATE TABLE IF NOT EXISTS talent_pool_members (
        member_id        VARCHAR(36) PRIMARY KEY,
        pool_id          VARCHAR(36) NOT NULL REFERENCES talent_pools(pool_id) ON DELETE CASCADE,
        profile_id       VARCHAR(36) NOT NULL REFERENCES profiles(profile_id),
        stage            VARCHAR(20) DEFAULT 'sourced',
        note             TEXT,
        added_by_user_id VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,
        created_at       TIMESTAMP NOT NULL DEFAULT now(),
        updated_at       TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pool_profile ON talent_pool_members (pool_id, profile_id)",
    "CREATE INDEX IF NOT EXISTS ix_pool_members_pool ON talent_pool_members (pool_id)",
    "CREATE INDEX IF NOT EXISTS ix_pool_members_stage ON talent_pool_members (stage)",
    "CREATE INDEX IF NOT EXISTS ix_pool_members_profile ON talent_pool_members (profile_id)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        pools = db.execute(text("SELECT count(*) FROM talent_pools")).scalar()
        members = db.execute(text("SELECT count(*) FROM talent_pool_members")).scalar()
        print(f"OK - talent_pools={pools} talent_pool_members={members}")
    finally:
        db.close()


if __name__ == "__main__":
    run()
