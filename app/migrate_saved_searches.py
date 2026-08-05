"""Create the saved_searches table (idempotent).

Run:  python -m app.migrate_saved_searches
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    """
    CREATE TABLE IF NOT EXISTS saved_searches (
        search_id       VARCHAR(36) PRIMARY KEY,
        owner_user_id   VARCHAR(36) NOT NULL REFERENCES users(user_id),
        name            VARCHAR(120) NOT NULL,
        params          JSON NOT NULL DEFAULT '{}',
        notify          BOOLEAN NOT NULL DEFAULT TRUE,
        last_count      INTEGER,
        last_checked_at TIMESTAMP,
        created_at      TIMESTAMP NOT NULL DEFAULT now(),
        updated_at      TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_search_owner_name ON saved_searches (owner_user_id, name)",
    "CREATE INDEX IF NOT EXISTS ix_saved_searches_owner ON saved_searches (owner_user_id)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        print("OK - saved_searches rows:",
              db.execute(text("SELECT count(*) FROM saved_searches")).scalar())
    finally:
        db.close()


if __name__ == "__main__":
    run()
