"""Job alerts for professionals, shared pools and submission tracking (idempotent).

Run:  python -m app.migrate_team_and_alerts
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal

DDL = [
    # A saved search now belongs to one side of the marketplace: recruiters save
    # candidate searches, professionals save job searches. Same alerting model.
    "ALTER TABLE saved_searches ADD COLUMN IF NOT EXISTS kind VARCHAR(20) DEFAULT 'providers'",
    "CREATE INDEX IF NOT EXISTS ix_saved_searches_kind ON saved_searches (kind)",

    # A pool can belong to the agency rather than one recruiter, so a team can
    # work the same shortlist.
    "ALTER TABLE talent_pools ADD COLUMN IF NOT EXISTS visibility VARCHAR(12) DEFAULT 'private'",
    "ALTER TABLE talent_pools ADD COLUMN IF NOT EXISTS employer_id VARCHAR(36) REFERENCES employers(employer_id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_talent_pools_visibility ON talent_pools (visibility)",
    "CREATE INDEX IF NOT EXISTS ix_talent_pools_employer ON talent_pools (employer_id)",

    # Submitting a candidate to a client facility is the agency's billable
    # event, and the thing pools stop short of recording.
    """
    CREATE TABLE IF NOT EXISTS submissions (
        submission_id  VARCHAR(36) PRIMARY KEY,
        profile_id     VARCHAR(36) NOT NULL REFERENCES profiles(profile_id),
        job_id         VARCHAR(36) REFERENCES job_postings(job_id) ON DELETE SET NULL,
        pool_id        VARCHAR(36) REFERENCES talent_pools(pool_id) ON DELETE SET NULL,
        employer_id    VARCHAR(36) REFERENCES employers(employer_id) ON DELETE SET NULL,
        facility       VARCHAR(200),
        submitted_by_user_id VARCHAR(36) NOT NULL REFERENCES users(user_id),
        status         VARCHAR(24) DEFAULT 'submitted',
        bill_rate      NUMERIC(10,2),
        pay_rate       NUMERIC(10,2),
        note           TEXT,
        submitted_at   TIMESTAMP NOT NULL DEFAULT now(),
        status_updated_at TIMESTAMP NOT NULL DEFAULT now(),
        created_at     TIMESTAMP NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_submissions_profile ON submissions (profile_id)",
    "CREATE INDEX IF NOT EXISTS ix_submissions_owner ON submissions (submitted_by_user_id)",
    "CREATE INDEX IF NOT EXISTS ix_submissions_status ON submissions (status)",
    "CREATE INDEX IF NOT EXISTS ix_submissions_facility ON submissions (facility)",
    # One live submission of a person to a role; re-submitting is an update.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_profile_job ON submissions (profile_id, job_id)",
]


def run() -> None:
    db = SessionLocal()
    try:
        for stmt in DDL:
            db.execute(text(stmt))
        db.commit()
        print("OK - submissions:",
              db.execute(text("SELECT count(*) FROM submissions")).scalar())
        print("     saved_searches.kind + talent_pools.visibility added")
    finally:
        db.close()


if __name__ == "__main__":
    run()
