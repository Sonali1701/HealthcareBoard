"""Fill in profile lat/lng from the local ZIP and city centroid tables.

Only ~1.5k of 160k providers had coordinates, so every radius search and the
matching engine's location score fell back to coarse state matching. Both
centroid tables ship with the database, so this is a pure set-based UPDATE —
no geocoding API, no per-row round trips.

ZIP wins over city: a ZIP centroid is far tighter than a city centroid.

Run:  python -m app.backfill_geocodes [--dry-run]
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from .database import SessionLocal

# Postgres normalises the ZIP to its first 5 digits before matching, so
# "48185-1234" still resolves.
_BY_ZIP = """
UPDATE profiles p
   SET lat = z.lat, lng = z.lng
  FROM zip_centroids z
 WHERE p.lat IS NULL
   AND p.zip_code IS NOT NULL
   AND z.zip = substring(regexp_replace(p.zip_code, '[^0-9]', '', 'g') from 1 for 5)
"""

_BY_CITY = """
UPDATE profiles p
   SET lat = c.lat, lng = c.lng
  FROM city_centroids c
 WHERE p.lat IS NULL
   AND p.city IS NOT NULL
   AND p.state_code IS NOT NULL
   AND c.city_lower = lower(btrim(p.city))
   AND c.state_code = upper(btrim(p.state_code))
"""


def _stats(db) -> tuple[int, int]:
    have = db.execute(text("SELECT count(*) FROM profiles WHERE lat IS NOT NULL")).scalar()
    total = db.execute(text("SELECT count(*) FROM profiles")).scalar()
    return have, total


def run(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        before, total = _stats(db)
        print(f"before: {before:,} / {total:,} profiles have coordinates")

        n_zip = db.execute(text(_BY_ZIP)).rowcount
        print(f"  matched by ZIP centroid : {n_zip:,}")
        n_city = db.execute(text(_BY_CITY)).rowcount
        print(f"  matched by city centroid: {n_city:,}")

        if dry_run:
            db.rollback()
            print("DRY RUN - rolled back")
            return
        db.commit()

        after, _ = _stats(db)
        print(f"after : {after:,} / {total:,} profiles have coordinates "
              f"(+{after - before:,})")
        left = db.execute(text(
            "SELECT count(*) FROM profiles WHERE lat IS NULL")).scalar()
        print(f"still without coordinates: {left:,} (no usable ZIP or city)")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
