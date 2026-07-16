"""Geocode profiles so the Providers directory can filter by ZIP + radius.

    python -m app.migrate_geocode

What it does (safe + idempotent):
  1. Adds profiles.zip_code and enables the Postgres cube + earthdistance
     extensions used for fast radius queries.
  2. Builds zip_centroids (zip -> lat/lng) and city_centroids (city+state ->
     lat/lng) reference tables from the free offline `pgeocode` dataset.
  3. Backfills profiles.lat/lng: by ZIP when present (precise), otherwise by
     city+state (city-center) — so distance search works on existing data.
  4. Adds a GiST index on ll_to_earth(lat,lng) so radius filters stay fast at
     millions of rows.

Re-run any time (e.g. after a big import) to geocode newly-added profiles.
"""
from __future__ import annotations

import re

from sqlalchemy import text

from .database import engine, SessionLocal

_IS_PG = engine.dialect.name == "postgresql"


def _schema() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS zip_code VARCHAR(10)"))
        if _IS_PG:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS cube"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS earthdistance"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS zip_centroids "
            "(zip TEXT PRIMARY KEY, lat DOUBLE PRECISION, lng DOUBLE PRECISION)"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS city_centroids "
            "(city_lower TEXT, state_code TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, "
            "PRIMARY KEY (city_lower, state_code))"))
    print("Schema ready: zip_code column, cube/earthdistance, centroid tables.")


def _populate_centroids() -> None:
    with engine.connect() as conn:
        have = conn.execute(text("SELECT count(*) FROM zip_centroids")).scalar() or 0
    if have:
        print(f"Centroids already populated ({have} zips) — skipping.")
        return
    import pgeocode

    nomi = pgeocode.Nominatim("us")
    df = nomi._data[["postal_code", "place_name", "state_code", "latitude", "longitude"]].copy()
    df = df.dropna(subset=["latitude", "longitude"])

    zips = df[["postal_code", "latitude", "longitude"]].drop_duplicates("postal_code")
    zips.columns = ["zip", "lat", "lng"]
    zips["zip"] = zips["zip"].astype(str).str.strip()

    df["city_lower"] = df["place_name"].astype(str).str.lower().str.strip()
    cities = (df.groupby(["city_lower", "state_code"], as_index=False)
                .agg(lat=("latitude", "mean"), lng=("longitude", "mean")))

    zips.to_sql("zip_centroids", engine, if_exists="append", index=False,
                chunksize=5000, method="multi")
    cities.to_sql("city_centroids", engine, if_exists="append", index=False,
                  chunksize=5000, method="multi")
    print(f"Loaded {len(zips)} zip centroids, {len(cities)} city centroids.")


def _backfill() -> None:
    with engine.begin() as conn:
        by_zip = conn.execute(text("""
            UPDATE profiles p SET lat = z.lat, lng = z.lng
            FROM zip_centroids z
            WHERE p.lat IS NULL AND p.zip_code IS NOT NULL AND p.zip_code = z.zip
        """)).rowcount
        by_city = conn.execute(text("""
            UPDATE profiles p SET lat = c.lat, lng = c.lng
            FROM city_centroids c
            WHERE p.lat IS NULL AND p.city IS NOT NULL AND p.state_code IS NOT NULL
              AND lower(trim(p.city)) = c.city_lower AND p.state_code = c.state_code
        """)).rowcount
    print(f"Geocoded: {by_zip} by ZIP, {by_city} by city+state.")


def _clean_city(raw: str | None) -> str | None:
    """Salvage a real city from the old parser's messy 'city' values, e.g.
    '442-236-3360 | El Centro' -> 'el centro', 'Wichita\\tFalls' -> 'wichita falls'."""
    if not raw:
        return None
    s = str(raw)
    if "|" in s:                       # phone/email | City  ->  keep the last part
        s = s.split("|")[-1]
    s = re.sub(r"[^A-Za-z .'-]", " ", s)     # drop digits/@/symbols
    s = re.sub(r"\s+", " ", s).strip()
    if re.fullmatch(r"(?:[A-Za-z] )+[A-Za-z]", s):   # 'B A R T O W' -> 'BARTOW'
        s = s.replace(" ", "")
    s = s.strip(" .'-").lower()
    return s if 2 <= len(s) <= 40 else None


def _backfill_messy() -> None:
    """Second pass: clean the junk 'city' strings and match them to a centroid."""
    db = SessionLocal()
    try:
        centroids = {
            (row[0], row[1]): (row[2], row[3])
            for row in db.execute(text("SELECT city_lower, state_code, lat, lng FROM city_centroids"))
        }
        rows = db.execute(text(
            "SELECT profile_id, city, state_code FROM profiles "
            "WHERE lat IS NULL AND city IS NOT NULL AND state_code IS NOT NULL")).all()
        updates = []
        for pid, city, state in rows:
            hit = centroids.get((_clean_city(city), state))
            if hit:
                updates.append({"pid": pid, "lat": hit[0], "lng": hit[1]})
        for i in range(0, len(updates), 1000):
            db.execute(text("UPDATE profiles SET lat=:lat, lng=:lng WHERE profile_id=:pid"),
                       updates[i:i + 1000])
        db.commit()
        print(f"Salvaged {len(updates)} more by cleaning messy city text.")
    finally:
        db.close()


def _index() -> None:
    if not _IS_PG:
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_profiles_earth "
                "ON profiles USING gist (ll_to_earth(lat, lng)) WHERE lat IS NOT NULL"))
            print("GiST radius index ix_profiles_earth ensured.")
        except Exception as e:  # noqa: BLE001
            print(f"Index step: {str(e)[:160]}")


def main() -> None:
    _schema()
    _populate_centroids()
    _backfill()
    _backfill_messy()
    _index()
    print("Done.")


if __name__ == "__main__":
    main()
