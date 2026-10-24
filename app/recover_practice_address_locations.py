"""Recover missing location from explicit source practice addresses.

Only complete_process records with no ZIP and incomplete city/state qualify.
The address must appear directly below a Practice Address header, and both the
ZIP and city/state centroids must agree within 100 miles. Every change is
audited. This is a provider's practice location, not a claimed home address.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = ROOT / "exports" / "practice_address_recovery_checkpoint.json"
START = "00000000-0000-0000-0000-000000000000"
EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
ADDRESS = re.compile(r"^([A-Za-z][A-Za-z .'-]{1,49}),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", re.I)
EXCLUDED = ("Other / Unclassified", "IT & Engineering",
            "Administrative & Management", "Business, Sales & Other Non-clinical")
SELECT = text("""
SELECT profile_id, city, state_code, resume_sections->>'raw_extracted_text' AS raw
FROM profiles
WHERE profile_id > :cursor AND capture_source = 'complete_process'
  AND job_category IS NOT NULL AND job_category NOT IN :excluded
  AND (lat IS NULL OR lng IS NULL)
  AND nullif(btrim(zip_code), '') IS NULL
  AND (nullif(btrim(city), '') IS NULL OR nullif(btrim(state_code), '') IS NULL)
  AND resume_sections->>'raw_extracted_text' LIKE '%Practice Address%'
ORDER BY profile_id LIMIT :take
""")
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS profile_practice_address_location_repairs_20261024 (
    profile_id varchar(36) PRIMARY KEY,
    old_city text, old_state_code text, old_zip_code text,
    old_lat double precision, old_lng double precision,
    new_city text NOT NULL, new_state_code text NOT NULL,
    new_zip_code text NOT NULL, new_lat double precision NOT NULL,
    new_lng double precision NOT NULL,
    evidence text NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT now()
)
""")
APPLY = text("""
WITH incoming AS (
    SELECT * FROM jsonb_to_recordset(CAST(:rows AS jsonb))
    AS x(profile_id text, city text, state_code text, zip_code text)
), verified AS (
    SELECT i.*, z.lat, z.lng
    FROM incoming i
    JOIN zip_centroids z ON z.zip = i.zip_code
    JOIN city_centroids c ON c.city_lower = lower(
        regexp_replace(i.city, '^St[.]? ', 'Saint ', 'i'))
        AND c.state_code = i.state_code
    WHERE power((z.lat-c.lat)*69, 2) + power((z.lng-c.lng)*54, 2) <= 10000
), logged AS (
    INSERT INTO profile_practice_address_location_repairs_20261024
    (profile_id, old_city, old_state_code, old_zip_code, old_lat, old_lng,
     new_city, new_state_code, new_zip_code, new_lat, new_lng, evidence)
    SELECT p.profile_id, p.city, p.state_code, p.zip_code, p.lat, p.lng,
           v.city, v.state_code, v.zip_code, v.lat, v.lng,
           'source_practice_address_zip_city_centroids_agree'
    FROM verified v JOIN profiles p ON p.profile_id = v.profile_id
    WHERE (p.lat IS NULL OR p.lng IS NULL)
      AND nullif(btrim(p.zip_code), '') IS NULL
      AND (nullif(btrim(p.city), '') IS NULL OR lower(btrim(p.city)) = lower(v.city))
      AND (nullif(btrim(p.state_code), '') IS NULL OR upper(btrim(p.state_code)) = v.state_code)
    ON CONFLICT (profile_id) DO NOTHING
    RETURNING profile_id, new_city, new_state_code, new_zip_code, new_lat, new_lng
), changed AS (
    UPDATE profiles p SET city = coalesce(nullif(btrim(p.city), ''), l.new_city),
        state_code = coalesce(nullif(btrim(p.state_code), ''), l.new_state_code),
        zip_code = l.new_zip_code, lat = l.new_lat, lng = l.new_lng,
        updated_at = now()
    FROM logged l WHERE p.profile_id = l.profile_id
      AND (p.lat IS NULL OR p.lng IS NULL)
    RETURNING p.profile_id
)
SELECT (SELECT count(*) FROM logged) AS logged,
       (SELECT count(*) FROM changed) AS changed
""")


def parse_address(raw: str) -> tuple[str, str, str] | None:
    marker = raw.find("Practice Address")
    if marker < 0:
        return None
    lines = raw[marker + len("Practice Address"):].splitlines()
    for line in lines[:5]:
        match = ADDRESS.fullmatch(line.strip())
        if match:
            city, state, zip_code = match.groups()
            return city.strip(), state.upper(), zip_code
    return None


def save(cursor: str, stats: Counter, complete: bool = False) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": cursor, "stats": dict(stats),
                               "complete": complete,
                               "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2),
                   encoding="utf-8")
    tmp.replace(CHECKPOINT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit-batches", type=int, default=0)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error("batch size must be 1..500")
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; recovery refused")
    cursor = START
    stats: Counter = Counter()
    if args.apply and CHECKPOINT.exists() and not args.restart:
        saved = json.loads(CHECKPOINT.read_text(encoding="utf-8"))
        cursor = saved.get("cursor", START)
        stats.update(saved.get("stats", {}))
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    from sqlalchemy import bindparam
    select = SELECT.bindparams(bindparam("excluded", expanding=True))
    if args.apply:
        with engine.begin() as conn:
            conn.execute(CREATE_AUDIT)
    batches = 0
    while True:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '120s'"))
            rows = conn.execute(select, {"cursor": cursor,
                                         "excluded": EXCLUDED,
                                         "take": args.batch_size}).mappings().all()
        if not rows:
            if args.apply:
                save(cursor, stats, complete=True)
            break
        incoming = []
        for row in rows:
            parsed = parse_address(row["raw"] or "")
            if not parsed:
                stats["no_explicit_address"] += 1
                continue
            city, state, zip_code = parsed
            if row["city"] and row["city"].strip().casefold() != city.casefold():
                stats["existing_city_disagrees"] += 1
                continue
            if row["state_code"] and row["state_code"].strip().upper() != state:
                stats["existing_state_disagrees"] += 1
                continue
            incoming.append({"profile_id": row["profile_id"], "city": city,
                             "state_code": state, "zip_code": zip_code})
        if args.apply and incoming:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                result = conn.execute(APPLY, {"rows": json.dumps(incoming)}).one()
                if result.logged != result.changed:
                    raise RuntimeError("Audit/location update mismatch")
            stats["recovered"] += result.changed
            stats["centroid_or_existing_field_rejected"] += len(incoming) - result.changed
        else:
            stats["address_candidates"] += len(incoming)
        stats["scanned"] += len(rows)
        cursor = rows[-1]["profile_id"]
        if args.apply:
            save(cursor, stats)
        print(json.dumps({"cursor": cursor, "stats": dict(stats)}), flush=True)
        batches += 1
        if args.limit_batches and batches >= args.limit_batches:
            break
        if not args.apply:
            break
    print(json.dumps({"complete": not rows, "stats": dict(stats)}), flush=True)


if __name__ == "__main__":
    main()
