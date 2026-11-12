"""Create and populate the separate NPI-backed provider directory.

The source archive is by_state.zip. Imported source columns are A:I and K:M;
the address, phone, raw credential and other-specialties columns are omitted.
Rows flagged by the source quality review are excluded before COPY.
"""
from __future__ import annotations

import csv
import io
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARCHIVE = Path(r"C:\Users\vs510\Downloads\by_state.zip")
ENV_FILE = Path(r"C:\Users\vs510\Downloads\HealthcareBoard.env")
DATABASE_NAME = "healthcare_provider_directory"
ZIP_RE = re.compile(r"^(\d{5})(?:-?(\d{4}))?$")
TAXONOMY_RE = re.compile(r"^[A-Z0-9]{10}$")
NPI_RE = re.compile(r"^\d{10}$")
VALID_US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC AS GU MP PR VI AA AE AP".split())
COPY_SQL = """COPY provider_directory
    (npi, first_name, middle_name, last_name, credential, gender, status,
     specialty, taxonomy_code, city, state_code, zip_code, lat, lng)
    FROM STDIN WITH (FORMAT CSV)"""


def env_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([^#=\s]+)\s*=\s*(.*?)\s*$", line)
        if match:
            result[match.group(1)] = match.group(2).strip().strip("\"'")
    return result


def direct_url(raw: str, database: str) -> str:
    raw = raw.replace("postgresql+psycopg://", "postgresql://", 1)
    parts = urlsplit(raw)
    host = parts.netloc.replace("-pooler.", ".")
    return urlunsplit((parts.scheme, host, f"/{database}", parts.query, parts.fragment))


def npi_is_valid(value: str) -> bool:
    if not NPI_RE.fullmatch(value):
        return False
    digits = "80840" + value[:9]
    total = 0
    for offset, char in enumerate(reversed(digits)):
        digit = int(char)
        if offset % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10 == int(value[-1])


def is_real_name(first: str, last: str) -> bool:
    """Keep in sync with app.importers.parsing.is_real_name."""
    from app.importers.parsing import is_real_name as halo_is_real_name
    return halo_is_real_name(first, last)


def parse_source_row(row: dict[str, str], centroids: dict[str, tuple[float, float]]):
    npi = (row.get("npi") or "").strip()
    first = (row.get("first_name") or "").strip()
    middle = (row.get("middle_name") or "").strip() or None
    last = (row.get("last_name") or "").strip()
    credential = (row.get("credential") or "").strip() or None
    gender = (row.get("gender") or "").strip() or None
    status = (row.get("status") or "").strip().upper()
    specialty = (row.get("specialty") or "").strip()
    taxonomy = (row.get("taxonomy_code") or "").strip().upper()
    city = (row.get("city") or "").strip()
    state = (row.get("state") or "").strip().upper()
    zip_raw = (row.get("zip") or "").strip()
    zip_match = ZIP_RE.fullmatch(zip_raw)
    zip5 = zip_match.group(1) if zip_match else ""

    reasons = []
    if not npi_is_valid(npi):
        reasons.append("invalid_npi")
    if not is_real_name(first, last):
        reasons.append("name_flag")
    if not specialty:
        reasons.append("missing_specialty")
    if not TAXONOMY_RE.fullmatch(taxonomy):
        reasons.append("invalid_taxonomy_code")
    if state not in VALID_US_STATES:
        reasons.append("invalid_state")
    if not zip_match or zip5 == "00000":
        reasons.append("invalid_or_missing_zip")
    if status != "ACTIVE":
        reasons.append("non_active_status")
    if not city:
        reasons.append("missing_city")
    if reasons:
        return None, reasons

    point = centroids.get(zip5)
    if point is None:
        return None, ["no_zip_centroid"]
    return (
        npi, first, middle, last, credential, gender, status,
        specialty, taxonomy, city, state, zip5,
        point[0] if point else None, point[1] if point else None,
    ), []


def main() -> int:
    if not ARCHIVE.is_file() or not ENV_FILE.is_file():
        raise SystemExit("Source ZIP or credential file is missing.")
    values = env_values(ENV_FILE)
    source_url = direct_url(values["DATABASE_URL"], "neondb")
    target_url = direct_url(values["DATABASE_URL"], DATABASE_NAME)

    start = time.monotonic()
    with psycopg.connect(target_url, connect_timeout=20) as target:
        with target.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='provider_directory'")
            exists = cur.fetchone()[0] > 0
            if exists:
                cur.execute("SELECT count(*) FROM provider_directory")
                if cur.fetchone()[0]:
                    raise SystemExit("Target provider_directory is already populated; refusing to append or overwrite.")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS provider_directory (
                    npi CHAR(10) PRIMARY KEY,
                    first_name TEXT NOT NULL,
                    middle_name TEXT,
                    last_name TEXT NOT NULL,
                    credential TEXT,
                    gender TEXT,
                    status TEXT NOT NULL CHECK (status = 'ACTIVE'),
                    specialty TEXT NOT NULL,
                    taxonomy_code CHAR(10) NOT NULL,
                    city TEXT NOT NULL,
                    state_code CHAR(2) NOT NULL,
                    zip_code CHAR(5) NOT NULL,
                    lat DOUBLE PRECISION,
                    lng DOUBLE PRECISION
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS zip_centroids (
                    zip CHAR(5) PRIMARY KEY,
                    lat DOUBLE PRECISION NOT NULL,
                    lng DOUBLE PRECISION NOT NULL
                )
            """)
        target.commit()

        centroids: dict[str, tuple[float, float]] = {}
        with psycopg.connect(source_url, connect_timeout=20, options="-c default_transaction_read_only=on") as source:
            with source.cursor(name="directory_zip_centroids") as cur:
                cur.itersize = 10000
                cur.execute("SELECT zip, lat, lng FROM public.zip_centroids WHERE lat IS NOT NULL AND lng IS NOT NULL")
                for zip_code, lat, lng in cur:
                    centroids[str(zip_code).strip()[:5]] = (float(lat), float(lng))
        with target.cursor() as cur:
            cur.execute("SELECT count(*) FROM zip_centroids")
            existing_centroids = cur.fetchone()[0]
            if existing_centroids:
                if existing_centroids != len(centroids):
                    raise RuntimeError(f"Target has {existing_centroids} ZIP centroids; expected {len(centroids)}.")
            else:
                with cur.copy("COPY zip_centroids (zip, lat, lng) FROM STDIN") as copy:
                    for zip_code, (lat, lng) in centroids.items():
                        copy.write_row((zip_code, lat, lng))
        target.commit()
        print(f"ZIP centroids ready: {len(centroids):,}.", flush=True)

        totals = Counter()
        reason_counts = Counter()
        valid_batch = []
        batch_size = 25000

        def flush(cur):
            if not valid_batch:
                return
            with cur.copy(COPY_SQL) as copy:
                buffer = io.StringIO()
                writer = csv.writer(buffer, lineterminator="\n")
                for record in valid_batch:
                    writer.writerow(record)
                copy.write(buffer.getvalue().encode("utf-8"))
            valid_batch.clear()

        with zipfile.ZipFile(ARCHIVE) as zf, target.cursor() as cur:
            csv_files = [f for f in zf.infolist() if f.filename.lower().endswith(".csv")]
            for file_no, info in enumerate(csv_files, start=1):
                with zf.open(info, "r") as raw, io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    if not reader.fieldnames or not {"npi", "first_name", "last_name", "specialty", "taxonomy_code", "city", "state", "zip"}.issubset(reader.fieldnames):
                        raise RuntimeError(f"Unexpected CSV header in {info.filename}")
                    for row in reader:
                        totals["rows_seen"] += 1
                        record, reasons = parse_source_row(row, centroids)
                        if reasons:
                            totals["rows_excluded"] += 1
                            for reason in reasons:
                                reason_counts[reason] += 1
                            continue
                        totals["rows_loaded"] += 1
                        if record[12] is not None:
                            totals["rows_geocoded"] += 1
                        else:
                            totals["rows_without_centroid"] += 1
                        valid_batch.append(record)
                        if len(valid_batch) >= batch_size:
                            flush(cur)
                target.commit()
                print(
                    f"File {file_no}/{len(csv_files)}: {info.filename}; "
                    f"seen {totals['rows_seen']:,}, loaded {totals['rows_loaded']:,}, "
                    f"excluded {totals['rows_excluded']:,}", flush=True,
                )
            flush(cur)
        target.commit()

        print("Creating search and filter indexes…", flush=True)
        with target.cursor() as cur:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                target.commit()
                cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_specialty_trgm_idx ON provider_directory USING gin (lower(specialty) gin_trgm_ops)")
                target.commit()
            except psycopg.Error as exc:
                target.rollback()
                print(f"Substring specialty index unavailable ({type(exc).__name__}); exact specialty filtering remains indexed.", flush=True)
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_specialty_idx ON provider_directory (specialty)")
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_taxonomy_idx ON provider_directory (taxonomy_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_state_idx ON provider_directory (state_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_zip_idx ON provider_directory (zip_code)")
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_location_idx ON provider_directory (lat, lng) WHERE lat IS NOT NULL AND lng IS NOT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_name_idx ON provider_directory (lower(last_name), lower(first_name), npi)")
            cur.execute("CREATE INDEX IF NOT EXISTS provider_directory_city_idx ON provider_directory (lower(city), state_code)")
            cur.execute("ANALYZE provider_directory")
        target.commit()

        with target.cursor() as cur:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE lat IS NOT NULL AND lng IS NOT NULL), count(DISTINCT specialty), count(DISTINCT state_code) FROM provider_directory")
            final = cur.fetchone()
            cur.execute("SELECT state_code, count(*) FROM provider_directory GROUP BY state_code ORDER BY state_code")
            state_counts = cur.fetchall()
        print(f"FINAL_TOTALS={final}", flush=True)
        print(f"EXCLUDED_REASONS={dict(reason_counts)}", flush=True)
        print(f"STATE_COUNT={len(state_counts)}", flush=True)
        print(f"ELAPSED_SECONDS={time.monotonic()-start:.1f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
