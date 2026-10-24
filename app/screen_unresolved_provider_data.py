"""Screen unresolved provider taxonomy from search, with a reversible audit.

The source-confirmed repairs should run first. This command never guesses a
profession or specialty. It only hides rows with a blank specialty or one of
the explicit profession/category conflicts from provider results.

    python -m app.screen_unresolved_provider_data
    python -m app.screen_unresolved_provider_data --apply
"""
from __future__ import annotations

import argparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
START = "00000000-0000-0000-0000-000000000000"
PREDICATES = {
    "profession_category_conflict_review": """
        (p.job_category = 'Nursing' AND p.profession_type IN ('MD','DO','PA'))
        OR (p.job_category = 'Physician' AND p.profession_type IN
            ('RN','PA','NP','LPN','CNA','CNM','CRNA'))
        OR (p.job_category = 'Physician Assistant' AND p.profession_type IN ('MD','DO','RN'))
        OR (p.job_category = 'Pharmacy' AND p.profession_type IN
            ('MD','DO','RN','PA','NP','LPN','CNA'))
        OR (p.job_category = 'Allied Health & Technicians'
            AND p.profession_type IN ('MD','DO'))
        OR (p.job_category = 'Facilities & Organizations'
            AND p.profession_type IN ('MD','DO'))
    """,
    "missing_specialty_review": """
        p.job_category IS NOT NULL
        AND p.job_category NOT IN ('Other / Unclassified', 'IT & Engineering',
            'Administrative & Management', 'Business, Sales & Other Non-clinical')
        AND nullif(btrim(p.specialty), '') IS NULL
    """,
}
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_unresolved_provider_screen_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    reason varchar(60) NOT NULL,
    old_is_listable boolean NOT NULL,
    old_screen_reason varchar(60),
    old_screened_at timestamptz,
    specialty text,
    job_category text,
    profession_type text,
    screened_at timestamptz NOT NULL DEFAULT now()
)
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 2000:
        parser.error("batch size must be 1..2000")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; screening refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    if args.apply:
        with engine.begin() as conn:
            conn.execute(CREATE_AUDIT)
    for reason, predicate in PREDICATES.items():
        cursor = START
        total = 0
        batch = text(f"""
            WITH candidates AS MATERIALIZED (
                SELECT p.profile_id FROM profiles p
                WHERE p.is_listable IS TRUE AND p.profile_id > :cursor
                  AND ({predicate})
                ORDER BY p.profile_id LIMIT :take
            ), logged AS (
                INSERT INTO profile_unresolved_provider_screen_20261024
                (profile_id, reason, old_is_listable, old_screen_reason,
                 old_screened_at, specialty, job_category, profession_type)
                SELECT p.profile_id, :reason, p.is_listable, p.screen_reason,
                       p.screened_at, p.specialty, p.job_category, p.profession_type
                FROM profiles p JOIN candidates c ON c.profile_id = p.profile_id
                WHERE p.is_listable IS TRUE
                ON CONFLICT (profile_id) DO NOTHING RETURNING profile_id
            ), hidden AS (
                UPDATE profiles p SET is_listable = false,
                    screen_reason = :reason, screened_at = now(), updated_at = now()
                FROM logged l WHERE p.profile_id = l.profile_id
                  AND p.is_listable IS TRUE AND ({predicate})
                RETURNING p.profile_id
            )
            SELECT (SELECT max(profile_id) FROM candidates) AS cursor,
                   (SELECT count(*) FROM candidates) AS scanned,
                   (SELECT count(*) FROM hidden) AS hidden
        """)
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '120s'"))
            count = conn.execute(text(f"""SELECT count(*) FROM profiles p
                WHERE p.is_listable IS TRUE AND ({predicate})""")).scalar_one()
        print(f"{reason}: {count:,} visible", flush=True)
        if not args.apply:
            continue
        while True:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                row = conn.execute(batch, {"cursor": cursor,
                                           "take": args.batch_size,
                                           "reason": reason}).one()
            if row.cursor is None:
                break
            if row.hidden != row.scanned:
                raise RuntimeError(f"Audit/screen mismatch: {reason}: {row}")
            cursor = row.cursor
            total += row.hidden
            print(f"{reason}: screened {total:,}", flush=True)
        print(f"{reason}: complete, {total:,} screened", flush=True)


if __name__ == "__main__":
    main()
