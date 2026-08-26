"""Add users.active_session_id — the pointer to an account's current active
login, used to enforce single-active-session (anti account-sharing).

    python -m app.migrate_single_session

Additive and safe to re-run.
"""
from __future__ import annotations

from sqlalchemy import text

from .database import engine


def main() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                              "active_session_id VARCHAR(36)"))
        else:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(users)"))}
            if "active_session_id" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN active_session_id VARCHAR(36)"))
    print("users.active_session_id ensured.")


if __name__ == "__main__":
    main()
