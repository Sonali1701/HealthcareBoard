"""Resumable healthcare profile geocoding from existing Neon centroids.

ZIP takes precedence when it agrees with an exact city/state centroid within
about 100 miles. When the two disagree, the city/state centroid is used instead.
Profiles with no usable reference remain without coordinates. No external API
or candidate PII is sent anywhere.

    python -m app.backfill_healthcare_geocodes --sample
    python -m app.backfill_healthcare_geocodes --apply --batch-size 10000
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from .config import settings


ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = ROOT / "exports" / "healthcare_geocode_checkpoint.json"
START = "00000000-0000-0000-0000-000000000000"
EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"

# These are the non-healthcare buckets in the imported job taxonomy. Profiles
# without a category are deliberately skipped until the resume repair decides
# whether they belong in the provider directory.
NON_HEALTHCARE = (
    "Other / Unclassified", "IT & Engineering", "Administrative & Management",
    "Business, Sales & Other Non-clinical",
)

SAMPLE = text("""
    WITH candidates AS (
        SELECT p.profile_id, p.zip_code, p.city, p.state_code
          FROM profiles p
         WHERE p.profile_id > :cursor
           AND p.job_category IS NOT NULL
           AND p.job_category NOT IN :non_healthcare
           AND (p.lat IS NULL OR p.lng IS NULL)
         ORDER BY p.profile_id LIMIT :batch_size
    ), located AS (
        SELECT c.profile_id,
               CASE WHEN z.lat IS NOT NULL AND (
                    cc.lat IS NULL OR power((z.lat-cc.lat)*69, 2)
                                      + power((z.lng-cc.lng)*54, 2) <= 10000
               ) THEN 'zip'
                    WHEN cc.lat IS NOT NULL THEN 'city'
                    ELSE 'unresolved' END AS method
          FROM candidates c
          LEFT JOIN zip_centroids z ON z.zip = substring(
              regexp_replace(c.zip_code, '[^0-9]', '', 'g') from 1 for 5)
          LEFT JOIN city_centroids cc ON cc.city_lower = lower(btrim(c.city))
                                     AND cc.state_code = upper(btrim(c.state_code))
    )
    SELECT method, count(*) FROM located GROUP BY method
""")

BATCH = text("""
    WITH candidates AS MATERIALIZED (
        SELECT p.profile_id, p.zip_code, p.city, p.state_code
          FROM profiles p
         WHERE p.profile_id > :cursor
           AND p.job_category IS NOT NULL
           AND p.job_category NOT IN :non_healthcare
           AND (p.lat IS NULL OR p.lng IS NULL)
         ORDER BY p.profile_id LIMIT :batch_size
    ), located AS MATERIALIZED (
        SELECT c.profile_id, c.zip_code, c.city, c.state_code,
               CASE WHEN z.lat IS NOT NULL AND (
                    cc.lat IS NULL OR power((z.lat-cc.lat)*69, 2)
                                      + power((z.lng-cc.lng)*54, 2) <= 10000
               ) THEN z.lat ELSE cc.lat END AS lat,
               CASE WHEN z.lat IS NOT NULL AND (
                    cc.lat IS NULL OR power((z.lat-cc.lat)*69, 2)
                                      + power((z.lng-cc.lng)*54, 2) <= 10000
               ) THEN z.lng ELSE cc.lng END AS lng,
               CASE WHEN z.lat IS NOT NULL AND (
                    cc.lat IS NULL OR power((z.lat-cc.lat)*69, 2)
                                      + power((z.lng-cc.lng)*54, 2) <= 10000
               ) THEN 'zip'
                    WHEN cc.lat IS NOT NULL THEN 'city'
                    ELSE 'unresolved' END AS method
          FROM candidates c
          LEFT JOIN zip_centroids z ON z.zip = substring(
              regexp_replace(c.zip_code, '[^0-9]', '', 'g') from 1 for 5)
          LEFT JOIN city_centroids cc ON cc.city_lower = lower(btrim(c.city))
                                     AND cc.state_code = upper(btrim(c.state_code))
    ), changed AS (
        UPDATE profiles p SET lat = l.lat, lng = l.lng
          FROM located l
         WHERE p.profile_id = l.profile_id
           AND (p.lat IS NULL OR p.lng IS NULL)
           AND p.zip_code IS NOT DISTINCT FROM l.zip_code
           AND p.city IS NOT DISTINCT FROM l.city
           AND p.state_code IS NOT DISTINCT FROM l.state_code
           AND l.lat IS NOT NULL AND l.lng IS NOT NULL
        RETURNING l.method
    )
    SELECT (SELECT max(profile_id) FROM candidates) AS last_profile_id,
           (SELECT count(*) FROM candidates) AS scanned,
           (SELECT count(*) FROM changed WHERE method = 'zip') AS by_zip,
           (SELECT count(*) FROM changed WHERE method = 'city') AS by_city
""")


def _save(cursor: str, stats: Counter, *, complete: bool = False) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": cursor, "stats": dict(stats),
                               "complete": complete,
                               "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2),
                   encoding="utf-8")
    tmp.replace(CHECKPOINT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--limit-batches", type=int, default=0)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if not args.sample and not args.apply:
        parser.error("choose --sample or --apply")
    if not 1 <= args.batch_size <= 50000:
        parser.error("batch size must be 1..50000")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; geocode backfill refused")
    cursor = START
    stats: Counter = Counter()
    if args.apply and CHECKPOINT.exists() and not args.restart:
        saved = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        cursor = saved.get("cursor", START)
        stats.update(saved.get("stats", {}))
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    params = {"cursor": cursor, "batch_size": args.batch_size,
              "non_healthcare": NON_HEALTHCARE}
    # SQLAlchemy expanding parameters are needed for the four-category IN list.
    from sqlalchemy import bindparam
    sample = SAMPLE.bindparams(bindparam("non_healthcare", expanding=True))
    batch = BATCH.bindparams(bindparam("non_healthcare", expanding=True))
    if args.sample:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '120s'"))
            print(dict(conn.execute(sample, params).all()), flush=True)
        return
    batches = 0
    batch_size = args.batch_size
    while True:
        params["batch_size"] = batch_size
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '300s'"))
                row = conn.execute(batch, params).one()
        except OperationalError as exc:
            if "statement timeout" not in str(exc).lower() or batch_size <= 100:
                raise
            batch_size = max(100, batch_size // 2)
            print(json.dumps({"retry_after_timeout": True,
                              "batch_size": batch_size, "cursor": cursor}), flush=True)
            continue
        if not row.last_profile_id:
            _save(cursor, stats, complete=True)
            break
        cursor = row.last_profile_id
        stats.update({"scanned": row.scanned, "zip": row.by_zip,
                      "city": row.by_city, "unresolved": row.scanned-row.by_zip-row.by_city})
        _save(cursor, stats)
        batches += 1
        print(json.dumps({"batches": batches, "cursor": cursor, "stats": dict(stats)}), flush=True)
        params["cursor"] = cursor
        if args.limit_batches and batches >= args.limit_batches:
            break
    print(json.dumps({"complete": not row.last_profile_id,
                      "stats": dict(stats)}, indent=2), flush=True)


if __name__ == "__main__":
    main()
