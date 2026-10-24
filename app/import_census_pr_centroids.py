"""Import official 2026 Census Puerto Rico ZCTA representative points.

Download the national 2026 Gazetteer ZCTA archive from:
https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/
and supply its extracted pipe-delimited text file with ``--file``.
Only Puerto Rico 006xx, 007xx, and 009xx ZIP areas are accepted. Existing
centroids are never overwritten. A source audit records every inserted point.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from .config import settings


EXPECTED_HOST = "ep-nameless-flower-ah7umr3s-pooler.c-3.us-east-1.aws.neon.tech"
SOURCE = "US_Census_2026_Gazetteer_ZCTA_representative_point"
CREATE_AUDIT = text("""
CREATE TABLE IF NOT EXISTS zip_centroid_source_audit_20261024 (
    zip text PRIMARY KEY,
    lat double precision NOT NULL,
    lng double precision NOT NULL,
    source text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
)
""")
INSERT = text("""
WITH added AS (
    INSERT INTO zip_centroids (zip, lat, lng)
    VALUES (:zip, :lat, :lng)
    ON CONFLICT (zip) DO NOTHING
    RETURNING zip, lat, lng
)
INSERT INTO zip_centroid_source_audit_20261024 (zip, lat, lng, source)
SELECT zip, lat, lng, :source FROM added
ON CONFLICT (zip) DO NOTHING
RETURNING zip
""")


def read_points(path: Path) -> list[dict]:
    points = []
    seen = set()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="|")
        needed = {"GEOID", "INTPTLAT", "INTPTLONG"}
        if not needed.issubset(reader.fieldnames or []):
            raise ValueError(f"Gazetteer columns missing: {needed - set(reader.fieldnames or [])}")
        for row in reader:
            zip_code = row["GEOID"]
            if len(zip_code) != 5 or not zip_code.isdigit() or not zip_code.startswith(
                    ("006", "007", "009")):
                continue
            lat, lng = float(row["INTPTLAT"]), float(row["INTPTLONG"])
            if not (17.8 <= lat <= 18.6 and -68.0 <= lng <= -65.0):
                raise ValueError(f"Puerto Rico coordinate outside bounds: {zip_code}")
            if zip_code in seen:
                raise ValueError(f"Duplicate ZIP area: {zip_code}")
            seen.add(zip_code)
            points.append({"zip": zip_code, "lat": lat, "lng": lng,
                           "source": SOURCE})
    return points


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    points = read_points(args.file)
    print(f"Validated {len(points)} Puerto Rico ZIP area points", flush=True)
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
    print(f"Inserted {added} new ZIP centroids with source audit", flush=True)


if __name__ == "__main__":
    main()
