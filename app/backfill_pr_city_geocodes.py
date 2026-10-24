"""Fill remaining Puerto Rico coordinates from audited Census urban places.

Only profiles with no matching ZIP centroid qualify. The Census urban-place
representative point is approximate; the source city/state remain unchanged.
"""
from __future__ import annotations

import argparse
import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
PREDICATE = """
    p.state_code = 'PR' AND p.job_category IS NOT NULL
    AND p.job_category NOT IN ('Other / Unclassified', 'IT & Engineering',
        'Administrative & Management', 'Business, Sales & Other Non-clinical')
    AND (p.lat IS NULL OR p.lng IS NULL)
    AND NOT EXISTS (SELECT 1 FROM zip_centroids z WHERE z.zip = substring(
        regexp_replace(p.zip_code, '[^0-9]', '', 'g') from 1 for 5))
"""
JOIN = """
    JOIN city_centroids c ON c.city_lower = lower(btrim(p.city))
        AND c.state_code = 'PR'
    JOIN city_centroid_source_audit_20261024 source
        ON source.city_lower = c.city_lower AND source.state_code = 'PR'
"""
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_pr_city_geocode_repairs_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    city text NOT NULL,
    old_lat double precision, old_lng double precision,
    new_lat double precision NOT NULL, new_lng double precision NOT NULL,
    evidence text NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT now()
)
""")
COUNT = text(f"SELECT count(*) FROM profiles p {JOIN} WHERE {PREDICATE}")
BATCH = text(f"""
WITH candidates AS MATERIALIZED (
    SELECT p.profile_id, p.city, c.lat, c.lng
    FROM profiles p {JOIN}
    WHERE {PREDICATE}
    LIMIT :take
    FOR UPDATE OF p SKIP LOCKED
), logged AS (
    INSERT INTO profile_pr_city_geocode_repairs_20261024
    (profile_id, city, old_lat, old_lng, new_lat, new_lng, evidence)
    SELECT p.profile_id, c.city, p.lat, p.lng, c.lat, c.lng,
           'US_Census_2026_PR_zona_urbana_representative_point'
    FROM candidates c JOIN profiles p ON p.profile_id = c.profile_id
    WHERE {PREDICATE}
    ON CONFLICT (profile_id) DO NOTHING
    RETURNING profile_id, new_lat, new_lng
), changed AS (
    UPDATE profiles p SET lat = l.new_lat, lng = l.new_lng, updated_at = now()
    FROM logged l WHERE p.profile_id = l.profile_id AND {PREDICATE}
    RETURNING p.profile_id
)
SELECT (SELECT count(*) FROM candidates) AS candidates,
       (SELECT count(*) FROM logged) AS logged,
       (SELECT count(*) FROM changed) AS changed
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
        raise RuntimeError("Unexpected database target; backfill refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    if not args.apply:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '120s'"))
            print(f"{conn.execute(COUNT).scalar_one()} PR profiles can be geocoded by city")
        return
    with engine.begin() as conn:
        conn.execute(CREATE_AUDIT)
    total = 0
    idle = 0
    while True:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            row = conn.execute(BATCH, {"take": args.batch_size}).one()
            if row.logged != row.changed or row.changed != row.candidates:
                raise RuntimeError(f"Audit/geocode mismatch: {row}")
        if not row.candidates:
            with engine.connect() as conn:
                remaining = conn.execute(COUNT).scalar_one()
            if not remaining:
                break
            idle += 1
            if idle >= 3:
                raise RuntimeError(f"{remaining} PR city profiles remain locked or unprocessed")
            time.sleep(2)
            continue
        idle = 0
        total += row.changed
        print(f"Geocoded {total} PR city profiles", flush=True)
    print(f"Complete: {total} geocoded", flush=True)


if __name__ == "__main__":
    main()
