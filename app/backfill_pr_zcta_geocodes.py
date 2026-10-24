"""Fill Puerto Rico healthcare coordinates from audited Census ZCTA points.

The imported ZIP area representative point is approximate. The address fields
are left unchanged. Use ``--apply`` for audited, resumable batches.
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
"""
JOIN = """
    JOIN zip_centroids z ON z.zip = substring(
        regexp_replace(p.zip_code, '[^0-9]', '', 'g') from 1 for 5)
    JOIN zip_centroid_source_audit_20261024 source ON source.zip = z.zip
"""
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_pr_zcta_geocode_repairs_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    zip text NOT NULL,
    old_lat double precision, old_lng double precision,
    new_lat double precision NOT NULL, new_lng double precision NOT NULL,
    evidence text NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT now()
)
""")
COUNT = text(f"SELECT count(*) FROM profiles p {JOIN} WHERE {PREDICATE}")
BATCH = text(f"""
WITH candidates AS MATERIALIZED (
    SELECT p.profile_id, z.zip, z.lat, z.lng
    FROM profiles p {JOIN}
    WHERE {PREDICATE}
    LIMIT :take
    FOR UPDATE OF p SKIP LOCKED
), logged AS (
    INSERT INTO profile_pr_zcta_geocode_repairs_20261024
    (profile_id, zip, old_lat, old_lng, new_lat, new_lng, evidence)
    SELECT p.profile_id, c.zip, p.lat, p.lng, c.lat, c.lng,
           'US_Census_2026_ZCTA_representative_point'
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
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 5000:
        parser.error("batch size must be 1..5000")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; backfill refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    if not args.apply:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '120s'"))
            print(f"{conn.execute(COUNT).scalar_one()} PR profiles can be geocoded")
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
                raise RuntimeError(f"{remaining} PR profiles remain locked or unprocessed")
            time.sleep(2)
            continue
        idle = 0
        total += row.changed
        print(f"Geocoded {total} PR healthcare profiles", flush=True)
    print(f"Complete: {total} geocoded", flush=True)


if __name__ == "__main__":
    main()
