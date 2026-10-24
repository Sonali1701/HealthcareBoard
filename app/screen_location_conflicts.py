"""Hide providers whose ZIP and city/state centroids disagree by >100 miles.

The source record must be reviewed to decide which field is wrong. Screening is
audited and reversible; this prevents an incorrect radius match in the interim.
"""
from __future__ import annotations

import argparse
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
REASON = "location_centroid_conflict_review"
JOIN = """
JOIN zip_centroids z ON z.zip = substring(
    regexp_replace(p.zip_code, '[^0-9]', '', 'g') from 1 for 5)
JOIN city_centroids c ON c.city_lower = lower(btrim(p.city))
    AND c.state_code = upper(btrim(p.state_code))
"""
PREDICATE = """
    p.is_listable IS TRUE AND p.job_category IS NOT NULL
    AND p.job_category NOT IN ('Other / Unclassified', 'IT & Engineering',
        'Administrative & Management', 'Business, Sales & Other Non-clinical')
    AND (p.lat IS NULL OR p.lng IS NULL)
    AND power((z.lat-c.lat)*69,2)+power((z.lng-c.lng)*54,2) > 10000
"""
COUNT = text(f"SELECT count(*) FROM profiles p {JOIN} WHERE {PREDICATE}")
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_location_conflict_screen_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    city text, state_code text, zip_code text,
    zip_lat double precision, zip_lng double precision,
    city_lat double precision, city_lng double precision,
    old_is_listable boolean NOT NULL,
    old_screen_reason varchar(60), old_screened_at timestamptz,
    screened_at timestamptz NOT NULL DEFAULT now()
)
""")
BATCH = text(f"""
WITH candidates AS MATERIALIZED (
    SELECT p.profile_id, p.city, p.state_code, p.zip_code,
           z.lat AS zip_lat, z.lng AS zip_lng,
           c.lat AS city_lat, c.lng AS city_lng
    FROM profiles p {JOIN} WHERE {PREDICATE}
    LIMIT :take FOR UPDATE OF p SKIP LOCKED
), logged AS (
    INSERT INTO profile_location_conflict_screen_20261024
    (profile_id, city, state_code, zip_code, zip_lat, zip_lng,
     city_lat, city_lng, old_is_listable, old_screen_reason, old_screened_at)
    SELECT p.profile_id, c.city, c.state_code, c.zip_code,
           c.zip_lat, c.zip_lng, c.city_lat, c.city_lng,
           p.is_listable, p.screen_reason, p.screened_at
    FROM candidates c JOIN profiles p ON p.profile_id = c.profile_id
    WHERE p.is_listable IS TRUE
    ON CONFLICT (profile_id) DO NOTHING RETURNING profile_id
), hidden AS (
    UPDATE profiles p SET is_listable = false, screen_reason = :reason,
        screened_at = now(), updated_at = now()
    FROM logged l WHERE p.profile_id = l.profile_id AND p.is_listable IS TRUE
    RETURNING p.profile_id
)
SELECT (SELECT count(*) FROM candidates) AS candidates,
       (SELECT count(*) FROM logged) AS logged,
       (SELECT count(*) FROM hidden) AS hidden
""")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 1000:
        parser.error("batch size must be 1..1000")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; screen refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = '120s'"))
        count = conn.execute(COUNT).scalar_one()
    print(f"{count} visible profiles have conflicting ZIP and city/state", flush=True)
    if not args.apply:
        return
    with engine.begin() as conn:
        conn.execute(CREATE_AUDIT)
    total = 0
    idle = 0
    while True:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            row = conn.execute(BATCH, {"take": args.batch_size,
                                       "reason": REASON}).one()
            if row.logged != row.hidden or row.hidden != row.candidates:
                raise RuntimeError(f"Audit/screen mismatch: {row}")
        if not row.candidates:
            with engine.connect() as conn:
                remaining = conn.execute(COUNT).scalar_one()
            if not remaining:
                break
            idle += 1
            if idle >= 3:
                raise RuntimeError(f"{remaining} location conflicts remain locked")
            time.sleep(2)
            continue
        idle = 0
        total += row.hidden
        print(f"Screened {total} location conflicts", flush=True)
    print(f"Complete: {total} screened", flush=True)


if __name__ == "__main__":
    main()
