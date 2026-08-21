"""Client facilities: the `clients` table + submissions.client_id (idempotent).

Run:  python -m app.migrate_clients

New databases get these from create_all/Alembic; this brings an existing
Postgres/Neon database up to date without recreating anything.
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    """
    CREATE TABLE IF NOT EXISTS clients (
        client_id      VARCHAR(36) PRIMARY KEY,
        owner_user_id  VARCHAR(36) NOT NULL REFERENCES users(user_id),
        employer_id    VARCHAR(36) REFERENCES employers(employer_id) ON DELETE SET NULL,
        name           VARCHAR(200) NOT NULL,
        facility_type  VARCHAR(80),
        city           VARCHAR(120),
        state_code     VARCHAR(2),
        website_url    VARCHAR(255),
        contact_name   VARCHAR(120),
        contact_email  VARCHAR(255),
        contact_phone  VARCHAR(40),
        default_bill_rate NUMERIC(10,2),
        notes          TEXT,
        is_active      BOOLEAN NOT NULL DEFAULT TRUE,
        created_at     TIMESTAMP NOT NULL DEFAULT now(),
        updated_at     TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_clients_owner ON clients (owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_clients_employer ON clients (employer_id)",
    "CREATE INDEX IF NOT EXISTS ix_clients_name ON clients (name)",
    "CREATE INDEX IF NOT EXISTS ix_clients_state ON clients (state_code)",
    "CREATE INDEX IF NOT EXISTS ix_clients_active ON clients (is_active)",
    # Link submissions to a managed client (kept nullable; free-text facility stays).
    "ALTER TABLE submissions ADD COLUMN IF NOT EXISTS client_id VARCHAR(36) "
    "REFERENCES clients(client_id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_submissions_client ON submissions (client_id)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        print("OK - clients:", db.execute(text("SELECT count(*) FROM clients")).scalar())
        print("     submissions.client_id added")
    finally:
        db.close()


if __name__ == "__main__":
    run()
