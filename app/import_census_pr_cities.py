"""Import exact Puerto Rico urban-place centroids from 2026 Census Gazetteer.

Supply the official ``2026_gaz_place_72.txt`` file with ``--file``. Only names
ending in "zona urbana" are mapped to their corresponding city name; community
names are excluded because many recur in different municipalities. Existing
centroids are never overwritten, and every addition is source audited.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
SOURCE = "US_Census_2026_Puerto_Rico_zona_urbana_representative_point"
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS city_centroid_source_audit_20261024 (
    city_lower text NOT NULL, state_code text NOT NULL,
    lat double precision NOT NULL, lng double precision NOT NULL,
    source text NOT NULL, imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (city_lower, state_code)
)
""")
INSERT = text("""
WITH added AS (
    INSERT INTO city_centroids (city_lower, state_code, lat, lng)
    VALUES (:city_lower, 'PR', :lat, :lng)
    ON CONFLICT (city_lower, state_code) DO NOTHING
    RETURNING city_lower, state_code, lat, lng
)
INSERT INTO city_centroid_source_audit_20261024
(city_lower, state_code, lat, lng, source)
SELECT city_lower, state_code, lat, lng, :source FROM added
ON CONFLICT (city_lower, state_code) DO NOTHING
RETURNING city_lower
""")


def read_points(path: Path) -> list[dict]:
    points = []
    seen = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="|")
        needed = {"USPS", "NAME", "LSAD", "INTPTLAT", "INTPTLONG"}
        if not needed.issubset(reader.fieldnames or []):
            raise ValueError(f"Gazetteer columns missing: {needed - set(reader.fieldnames or [])}")
        for row in reader:
            name = row["NAME"]
            if row["USPS"] != "PR" or row["LSAD"] != "62" or not name.endswith(" zona urbana"):
                continue
            city = name.removesuffix(" zona urbana").strip().lower()
            lat, lng = float(row["INTPTLAT"]), float(row["INTPTLONG"])
            if not (17.8 <= lat <= 18.6 and -68.0 <= lng <= -65.0):
                raise ValueError(f"Puerto Rico coordinate outside bounds: {city}")
            if city in seen:
                raise ValueError(f"Duplicate urban place: {city}")
            seen.add(city)
            points.append({"city_lower": city, "lat": lat, "lng": lng,
                           "source": SOURCE})
    return points


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    points = read_points(args.file)
    print(f"Validated {len(points)} unique Puerto Rico urban places", flush=True)
    if not args.apply:
        return
    url = make_url(settings.database_url)
    if url.host != EXPECTED_HOST or url.database != "neondb":
        raise RuntimeError("Unexpected database target; import refused")
    engine = create_engine(settings.database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 8})
    with engine.begin() as conn:
        conn.execute(CREATE_AUDIT)
        added = 0
        for point in points:
            added += len(conn.execute(INSERT, point).all())
    print(f"Inserted {added} new city centroids with source audit", flush=True)


if __name__ == "__main__":
    main()
