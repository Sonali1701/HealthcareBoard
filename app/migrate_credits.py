"""Create the credit tables (idempotent).

Run:  python -m app.migrate_credits
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    """
    CREATE TABLE IF NOT EXISTS credit_accounts (
        account_id       VARCHAR(36) PRIMARY KEY,
        user_id          VARCHAR(36) NOT NULL REFERENCES users(user_id),
        balance          INTEGER NOT NULL DEFAULT 0,
        lifetime_granted INTEGER NOT NULL DEFAULT 0,
        lifetime_spent   INTEGER NOT NULL DEFAULT 0,
        created_at       TIMESTAMP NOT NULL DEFAULT now(),
        updated_at       TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_account_user ON credit_accounts (user_id)",
    # A balance may never go negative, whatever the application does.
    """
    DO $$ BEGIN
        ALTER TABLE credit_accounts ADD CONSTRAINT ck_credit_balance_non_negative
            CHECK (balance >= 0);
    EXCEPTION WHEN duplicate_object THEN NULL; END $$
    """,
    """
    CREATE TABLE IF NOT EXISTS credit_transactions (
        txn_id          VARCHAR(36) PRIMARY KEY,
        account_id      VARCHAR(36) NOT NULL REFERENCES credit_accounts(account_id) ON DELETE CASCADE,
        user_id         VARCHAR(36) NOT NULL REFERENCES users(user_id),
        delta           INTEGER NOT NULL,
        balance_after   INTEGER NOT NULL,
        reason          VARCHAR(30),
        action          VARCHAR(40),
        entity_type     VARCHAR(40),
        entity_id       VARCHAR(36),
        idempotency_key VARCHAR(160),
        note            TEXT,
        created_at      TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_txn_idem ON credit_transactions (idempotency_key)",
    "CREATE INDEX IF NOT EXISTS ix_credit_txn_account ON credit_transactions (account_id)",
    "CREATE INDEX IF NOT EXISTS ix_credit_txn_entity ON credit_transactions (entity_id)",
    "CREATE INDEX IF NOT EXISTS ix_credit_txn_action ON credit_transactions (action)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        for t in ("credit_accounts", "credit_transactions"):
            print(f"  {t}: {db.execute(text(f'SELECT count(*) FROM {t}')).scalar()} rows")
        print("OK")
    finally:
        db.close()


if __name__ == "__main__":
    run()
