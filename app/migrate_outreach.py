"""Create the outreach tables (idempotent).

Run:  python -m app.migrate_outreach
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    """
    CREATE TABLE IF NOT EXISTS email_templates (
        template_id   VARCHAR(36) PRIMARY KEY,
        owner_user_id VARCHAR(36) NOT NULL REFERENCES users(user_id),
        name          VARCHAR(120) NOT NULL,
        subject       VARCHAR(300) NOT NULL,
        body          TEXT NOT NULL,
        created_at    TIMESTAMP NOT NULL DEFAULT now(),
        updated_at    TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_email_template_owner_name ON email_templates (owner_user_id, name)",
    """
    CREATE TABLE IF NOT EXISTS outreach_campaigns (
        campaign_id   VARCHAR(36) PRIMARY KEY,
        owner_user_id VARCHAR(36) NOT NULL REFERENCES users(user_id),
        name          VARCHAR(150) NOT NULL,
        pool_id       VARCHAR(36) REFERENCES talent_pools(pool_id) ON DELETE SET NULL,
        template_id   VARCHAR(36) REFERENCES email_templates(template_id) ON DELETE SET NULL,
        subject       VARCHAR(300) NOT NULL,
        body          TEXT NOT NULL,
        status        VARCHAR(20) DEFAULT 'draft',
        total         INTEGER DEFAULT 0,
        sent          INTEGER DEFAULT 0,
        skipped       INTEGER DEFAULT 0,
        failed        INTEGER DEFAULT 0,
        opened        INTEGER DEFAULT 0,
        replied       INTEGER DEFAULT 0,
        created_at    TIMESTAMP NOT NULL DEFAULT now(),
        updated_at    TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_outreach_campaigns_owner ON outreach_campaigns (owner_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_outreach_campaigns_pool ON outreach_campaigns (pool_id)",
    "CREATE INDEX IF NOT EXISTS ix_outreach_campaigns_status ON outreach_campaigns (status)",
    """
    CREATE TABLE IF NOT EXISTS outreach_messages (
        message_id  VARCHAR(36) PRIMARY KEY,
        campaign_id VARCHAR(36) NOT NULL REFERENCES outreach_campaigns(campaign_id) ON DELETE CASCADE,
        profile_id  VARCHAR(36) NOT NULL REFERENCES profiles(profile_id),
        to_email    VARCHAR(255),
        subject     VARCHAR(300),
        body        TEXT,
        status      VARCHAR(20) DEFAULT 'queued',
        reason      VARCHAR(80),
        token       VARCHAR(48) NOT NULL,
        sent_at     TIMESTAMP,
        opened_at   TIMESTAMP,
        replied_at  TIMESTAMP,
        created_at  TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_message_token ON outreach_messages (token)",
    "CREATE INDEX IF NOT EXISTS ix_outreach_messages_campaign ON outreach_messages (campaign_id)",
    "CREATE INDEX IF NOT EXISTS ix_outreach_messages_status ON outreach_messages (status)",
    "CREATE INDEX IF NOT EXISTS ix_outreach_messages_email ON outreach_messages (to_email)",
    """
    CREATE TABLE IF NOT EXISTS outreach_suppressions (
        suppression_id VARCHAR(36) PRIMARY KEY,
        email          VARCHAR(255) NOT NULL,
        reason         VARCHAR(60) DEFAULT 'unsubscribed',
        created_at     TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_suppression_email ON outreach_suppressions (lower(email))",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        for t in ("email_templates", "outreach_campaigns", "outreach_messages",
                  "outreach_suppressions"):
            print(f"  {t}: {db.execute(text(f'SELECT count(*) FROM {t}')).scalar()} rows")
        print("OK")
    finally:
        db.close()


if __name__ == "__main__":
    run()
