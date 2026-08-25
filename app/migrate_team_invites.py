"""Create the team_invites table (idempotent).

Run:  python -m app.migrate_team_invites

New databases get this from create_all; this brings an existing Postgres/Neon
database up to date so enterprise team invitations work.
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    """
    CREATE TABLE IF NOT EXISTS team_invites (
        invite_id          VARCHAR(36) PRIMARY KEY,
        employer_id        VARCHAR(36) NOT NULL REFERENCES employers(employer_id),
        email              VARCHAR(255) NOT NULL,
        role               VARCHAR(50) NOT NULL DEFAULT 'recruiter',
        token_hash         VARCHAR(64) NOT NULL,
        status             VARCHAR(20) NOT NULL DEFAULT 'pending',
        invited_by_user_id VARCHAR(36) REFERENCES users(user_id) ON DELETE SET NULL,
        created_at         TIMESTAMP NOT NULL DEFAULT now(),
        expires_at         TIMESTAMP NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_team_invites_token ON team_invites (token_hash)",
    "CREATE INDEX IF NOT EXISTS ix_team_invites_email ON team_invites (email)",
    "CREATE INDEX IF NOT EXISTS ix_team_invites_status ON team_invites (status)",
    "CREATE INDEX IF NOT EXISTS ix_team_invites_employer ON team_invites (employer_id)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        print("OK - team_invites table ready")
    finally:
        db.close()


if __name__ == "__main__":
    run()
