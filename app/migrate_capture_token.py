"""Add users.capture_token — the extension's long-lived per-recruiter auth token.

    python -m app.migrate_capture_token

Safe to re-run.
"""
from __future__ import annotations

from sqlalchemy import text

from .database import engine


def main() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                              "capture_token VARCHAR(64)"))
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_capture_token "
                              "ON users (capture_token)"))
        else:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(users)"))}
            if "capture_token" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN capture_token VARCHAR(64)"))
    print("users.capture_token ensured.")


if __name__ == "__main__":
    main()
