"""Geocode St.-abbreviated cities from exact Saint city/state centroids."""
from __future__ import annotations

import argparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
PREDICATE = """
    p.job_category IS NOT NULL AND p.job_category NOT IN
        ('Other / Unclassified', 'IT & Engineering',
         'Administrative & Management', 'Business, Sales & Other Non-clinical')
    AND (p.lat IS NULL OR p.lng IS NULL)
    AND nullif(btrim(p.zip_code), '') IS NULL
    AND p.city ~* '^St[.]? '
    AND nullif(btrim(p.state_code), '') IS NOT NULL
"""
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_saint_city_geocode_repairs_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    old_lat double precision, old_lng double precision,
    city text NOT NULL, state_code text NOT NULL,
    new_lat double precision NOT NULL, new_lng double precision NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT now()
)
""")
DRY_COUNT = text(f"""
SELECT count(*) FROM profiles p JOIN city_centroids c
  ON c.city_lower = lower(regexp_replace(btrim(p.city), '^St[.]? ', 'Saint ', 'i'))
 AND c.state_code = upper(btrim(p.state_code))
WHERE {PREDICATE}
""")
BATCH = text(f"""
WITH candidates AS MATERIALIZED (
    SELECT p.profile_id, c.lat, c.lng FROM profiles p
    JOIN city_centroids c
      ON c.city_lower = lower(regexp_replace(btrim(p.city), '^St[.]? ', 'Saint ', 'i'))
     AND c.state_code = upper(btrim(p.state_code))
    WHERE {PREDICATE}
    LIMIT :take
), logged AS (
    INSERT INTO profile_saint_city_geocode_repairs_20261024
    (profile_id, old_lat, old_lng, city, state_code, new_lat, new_lng)
    SELECT p.profile_id, p.lat, p.lng, p.city, p.state_code, c.lat, c.lng
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
        raise RuntimeError("Unexpected database target; repair refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    total = 0
    if not args.apply:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '120s'"))
            count = conn.execute(DRY_COUNT).scalar_one()
        print(f"{count} St. city locations can be recovered")
        return
    if args.apply:
        with engine.begin() as conn:
            conn.execute(CREATE_AUDIT)
    while True:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '120s'"))
            row = conn.execute(BATCH, {"take": args.batch_size}).one()
            if row.logged != row.changed:
                raise RuntimeError("Audit/geocode update mismatch")
            if args.apply and row.candidates != row.changed:
                raise RuntimeError("Some candidate rows could not be audited")
        if not row.candidates:
            break
        total += row.changed
        print(f"Geocoded {total}", flush=True)
    if args.apply:
        print(f"Complete: {total} St. city coordinates recovered", flush=True)


if __name__ == "__main__":
    main()
