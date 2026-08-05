"""
Database store for candidates, jobs, pipeline, outreach, and do-not-contact.

Neon/PostgreSQL is used when DATABASE_URL is configured. SQLite remains the
zero-configuration fallback for local demos and tests.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

PIPELINE_STAGES = ["new", "enriched", "contacted", "replied", "submitted", "rejected"]

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, location TEXT,
  description TEXT, created REAL);
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, location TEXT,
  job_id INTEGER, stage TEXT DEFAULT 'new', fit_score REAL DEFAULT 0,
  phones TEXT DEFAULT '[]', emails TEXT DEFAULT '[]', addresses TEXT DEFAULT '[]',
  enrich_status TEXT DEFAULT 'pending', confidence REAL DEFAULT 0,
  verification TEXT DEFAULT '{}',
  notes TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '',
  source_id TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS outreach(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, channel TEXT,
  subject TEXT, body TEXT, status TEXT DEFAULT 'draft', created REAL);
CREATE TABLE IF NOT EXISTS dnc(
  id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT UNIQUE, reason TEXT, created REAL);
CREATE TABLE IF NOT EXISTS resumes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
  filename TEXT NOT NULL, mime_type TEXT DEFAULT 'application/pdf',
  size INTEGER DEFAULT 0, data BLOB NOT NULL,
  storage_provider TEXT DEFAULT 'database', object_key TEXT DEFAULT '',
  bucket TEXT DEFAULT '', public_url TEXT DEFAULT '',
  checksum_sha256 TEXT DEFAULT '', etag TEXT DEFAULT '', created REAL);
CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source, source_id);
CREATE INDEX IF NOT EXISTS idx_resumes_candidate ON resumes(candidate_id);
"""

_POSTGRES_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS jobs(
         id BIGSERIAL PRIMARY KEY, title TEXT, location TEXT,
         description TEXT, created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS candidates(
         id BIGSERIAL PRIMARY KEY, name TEXT, location TEXT,
         job_id BIGINT, stage TEXT DEFAULT 'new', fit_score DOUBLE PRECISION DEFAULT 0,
         phones TEXT DEFAULT '[]', emails TEXT DEFAULT '[]', addresses TEXT DEFAULT '[]',
         enrich_status TEXT DEFAULT 'pending', confidence DOUBLE PRECISION DEFAULT 0,
         verification TEXT DEFAULT '{}',
         notes TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '',
         source_id TEXT DEFAULT '', created DOUBLE PRECISION, updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS outreach(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT, channel TEXT,
         subject TEXT, body TEXT, status TEXT DEFAULT 'draft', created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS dnc(
         id BIGSERIAL PRIMARY KEY, value TEXT UNIQUE, reason TEXT, created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS resumes(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL,
         filename TEXT NOT NULL, mime_type TEXT DEFAULT 'application/pdf',
         size BIGINT DEFAULT 0, data BYTEA NOT NULL, created DOUBLE PRECISION
       )""",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source_url TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source_id TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS verification TEXT DEFAULT '{}'",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS storage_provider TEXT DEFAULT 'database'",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS object_key TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS bucket TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS public_url TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS checksum_sha256 TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS etag TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source, source_id)",
    "CREATE INDEX IF NOT EXISTS idx_resumes_candidate ON resumes(candidate_id)",
)

_POSTGRES_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()
_CANDIDATE_FIELDS = {
    "name", "location", "job_id", "stage", "fit_score", "phones", "emails",
    "addresses", "enrich_status", "confidence", "notes", "source",
    "source_url", "source_id", "verification",
}


class _Connection:
    def __init__(self, raw, postgres=False):
        self.raw = raw
        self.postgres = postgres

    def execute(self, query, args=()):
        if self.postgres:
            query = query.replace("?", "%s")
        return self.raw.execute(query, args)


def backend_name():
    return "postgresql" if config.DATABASE_URL else "sqlite"


def _prepare_postgres(connection):
    global _POSTGRES_SCHEMA_READY
    if _POSTGRES_SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _POSTGRES_SCHEMA_READY:
            return
        for statement in _POSTGRES_SCHEMA:
            connection.execute(statement)
        connection.raw.commit()
        _POSTGRES_SCHEMA_READY = True


@contextmanager
def _conn():
    if config.DATABASE_URL:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency error is explicit
            raise RuntimeError(
                "DATABASE_URL is configured but psycopg is not installed. "
                "Run: pip install -r requirements.txt"
            ) from exc

        raw = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
        connection = _Connection(raw, postgres=True)
        try:
            _prepare_postgres(connection)
            yield connection
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
        return

    raw = sqlite3.connect(config.DB_PATH)
    connection = _Connection(raw)
    try:
        raw.row_factory = sqlite3.Row
        raw.executescript(_SQLITE_SCHEMA)
        columns = {row["name"] for row in raw.execute("PRAGMA table_info(candidates)")}
        for name, definition in (
            ("source", "TEXT DEFAULT ''"),
            ("source_url", "TEXT DEFAULT ''"),
            ("source_id", "TEXT DEFAULT ''"),
            ("verification", "TEXT DEFAULT '{}'"),
        ):
            if name not in columns:
                raw.execute(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")
        resume_columns = {row["name"] for row in raw.execute("PRAGMA table_info(resumes)")}
        for name, definition in (
            ("storage_provider", "TEXT DEFAULT 'database'"),
            ("object_key", "TEXT DEFAULT ''"),
            ("bucket", "TEXT DEFAULT ''"),
            ("public_url", "TEXT DEFAULT ''"),
            ("checksum_sha256", "TEXT DEFAULT ''"),
            ("etag", "TEXT DEFAULT ''"),
        ):
            if name not in resume_columns:
                raw.execute(f"ALTER TABLE resumes ADD COLUMN {name} {definition}")
        yield connection
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _row(row):
    data = dict(row)
    for key in ("phones", "emails", "addresses", "verification"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except Exception:
                data[key] = {} if key == "verification" else []
    return data


def _scalar(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _insert_id(connection, query, args):
    if connection.postgres:
        row = connection.execute(f"{query} RETURNING id", args).fetchone()
        return row["id"]
    return connection.execute(query, args).lastrowid


# ---- jobs ----
def create_job(title, location="", description=""):
    with _conn() as connection:
        return _insert_id(
            connection,
            "INSERT INTO jobs(title,location,description,created) VALUES(?,?,?,?)",
            (title, location, description, time.time()),
        )


def list_jobs():
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM jobs ORDER BY created DESC"
        )]


def get_job(job_id):
    with _conn() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


# ---- candidates ----
def add_candidate(
    name,
    location="",
    job_id=None,
    notes="",
    source="",
    source_url="",
    source_id="",
):
    now = time.time()
    with _conn() as connection:
        return _insert_id(
            connection,
            """INSERT INTO candidates(
                 name,location,job_id,notes,source,source_url,source_id,created,updated
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                name.strip(), location.strip(), job_id, notes.strip(),
                source.strip(), source_url.strip(), source_id.strip(), now, now,
            ),
        )


def add_candidates_bulk(rows, job_id=None):
    """rows: list of candidate dictionaries. Returns inserted IDs."""
    ids = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if name:
            ids.append(add_candidate(
                name,
                row.get("location", ""),
                job_id,
                notes=row.get("notes", ""),
                source=row.get("source", ""),
                source_url=row.get("source_url", ""),
                source_id=row.get("source_id", ""),
            ))
    return ids


def list_candidates(job_id=None, stage=None):
    query = "SELECT * FROM candidates"
    conditions, args = [], []
    if job_id is not None:
        conditions.append("job_id=?")
        args.append(job_id)
    if stage:
        conditions.append("stage=?")
        args.append(stage)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY fit_score DESC, created DESC"
    with _conn() as connection:
        return [_row(row) for row in connection.execute(query, args)]


def get_candidate(candidate_id):
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        return _row(row) if row else None


def get_candidate_by_source(source, source_id):
    if not source or not source_id:
        return None
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM candidates
               WHERE source=? AND source_id=?
               ORDER BY created DESC LIMIT 1""",
            (source.strip(), source_id.strip()),
        ).fetchone()
        return _row(row) if row else None


def get_candidate_by_identity(source, name, location=""):
    if not source or not name:
        return None
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM candidates
               WHERE LOWER(source)=LOWER(?) AND LOWER(name)=LOWER(?)
                 AND LOWER(COALESCE(location,''))=LOWER(?)
               ORDER BY created DESC LIMIT 1""",
            (source.strip(), name.strip(), location.strip()),
        ).fetchone()
        return _row(row) if row else None


def upsert_candidate_profiles(profiles, default_job_id=None):
    """Insert or refresh captured profiles in one database transaction."""
    results = []
    now = time.time()
    with _conn() as connection:
        for profile in profiles:
            name = (profile.get("name") or "").strip()
            location = (profile.get("location") or "").strip()
            source = (profile.get("source") or "indeed").strip().lower()
            source_id = (profile.get("source_id") or "").strip()
            source_url = (profile.get("source_url") or "").strip()
            notes = (profile.get("notes") or "").strip()
            job_id = profile.get("job_id")
            if job_id is None:
                job_id = default_job_id

            existing_row = None
            if source_id:
                existing_row = connection.execute(
                    """SELECT * FROM candidates
                       WHERE source=? AND source_id=?
                       ORDER BY created DESC LIMIT 1""",
                    (source, source_id),
                ).fetchone()
            if existing_row is None:
                existing_row = connection.execute(
                    """SELECT * FROM candidates
                       WHERE LOWER(source)=LOWER(?) AND LOWER(name)=LOWER(?)
                         AND LOWER(COALESCE(location,''))=LOWER(?)
                       ORDER BY created DESC LIMIT 1""",
                    (source, name, location),
                ).fetchone()

            if existing_row is not None:
                existing = _row(existing_row)
                updates = {"updated": now}
                if notes and len(notes) > len(existing.get("notes") or ""):
                    updates["notes"] = notes
                if location and not existing.get("location"):
                    updates["location"] = location
                if source_url:
                    updates["source_url"] = source_url
                if source_id and not existing.get("source_id"):
                    updates["source_id"] = source_id
                if job_id is not None and existing.get("job_id") is None:
                    updates["job_id"] = job_id
                assignments = ", ".join(f"{key}=?" for key in updates)
                connection.execute(
                    f"UPDATE candidates SET {assignments} WHERE id=?",
                    (*updates.values(), existing["id"]),
                )
                refreshed = connection.execute(
                    "SELECT * FROM candidates WHERE id=?", (existing["id"],)
                ).fetchone()
                results.append({
                    "id": existing["id"],
                    "imported": False,
                    "candidate": _row(refreshed),
                })
                continue

            candidate_id = _insert_id(
                connection,
                """INSERT INTO candidates(
                     name,location,job_id,notes,source,source_url,source_id,created,updated
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (name, location, job_id, notes, source, source_url, source_id, now, now),
            )
            inserted = connection.execute(
                "SELECT * FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            results.append({
                "id": candidate_id,
                "imported": True,
                "candidate": _row(inserted),
            })
    return results


def update_candidate(candidate_id, **fields):
    if not fields:
        return
    unknown = set(fields) - _CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"invalid candidate fields: {', '.join(sorted(unknown))}")
    for key in ("phones", "emails", "addresses", "verification"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])
    fields["updated"] = time.time()
    assignments = ", ".join(f"{key}=?" for key in fields)
    with _conn() as connection:
        connection.execute(
            f"UPDATE candidates SET {assignments} WHERE id=?",
            (*fields.values(), candidate_id),
        )


def set_stage(candidate_id, stage):
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"invalid stage {stage}")
    update_candidate(candidate_id, stage=stage)


# ---- resumes ----
def attach_resume(
    candidate_id,
    filename,
    data,
    mime_type="application/pdf",
    *,
    size=None,
    storage_provider="database",
    object_key="",
    bucket="",
    public_url="",
    checksum_sha256="",
    etag="",
):
    if not get_candidate(candidate_id):
        raise ValueError("candidate not found")
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("resume data must be bytes")
    if storage_provider == "database" and not data:
        raise ValueError("resume data is required")
    if storage_provider != "database" and not object_key:
        raise ValueError("cloud resume object key is required")
    stored_size = int(size if size is not None else len(data))
    now = time.time()
    with _conn() as connection:
        resume_id = _insert_id(
            connection,
            """INSERT INTO resumes(
                 candidate_id,filename,mime_type,size,data,storage_provider,
                 object_key,bucket,public_url,checksum_sha256,etag,created
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                candidate_id, filename, mime_type, stored_size, bytes(data),
                storage_provider, object_key, bucket, public_url,
                checksum_sha256, etag, now,
            ),
        )
    return {
        "id": resume_id,
        "candidate_id": candidate_id,
        "filename": filename,
        "mime_type": mime_type,
        "size": stored_size,
        "storage_provider": storage_provider,
        "object_key": object_key,
        "bucket": bucket,
        "public_url": public_url,
        "checksum_sha256": checksum_sha256,
        "created": now,
    }


def list_resumes(candidate_id):
    with _conn() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT id,candidate_id,filename,mime_type,size,storage_provider,
                          object_key,bucket,public_url,checksum_sha256,etag,created
                   FROM resumes WHERE candidate_id=? ORDER BY created DESC""",
                (candidate_id,),
            )
        ]


def get_resume_by_checksum(candidate_id, checksum_sha256):
    checksum = str(checksum_sha256 or "").strip().lower()
    if not checksum:
        return None
    with _conn() as connection:
        row = connection.execute(
            """SELECT id,candidate_id,filename,mime_type,size,storage_provider,
                      object_key,bucket,public_url,checksum_sha256,etag,created
               FROM resumes
               WHERE candidate_id=? AND LOWER(checksum_sha256)=?
               ORDER BY created DESC LIMIT 1""",
            (candidate_id, checksum),
        ).fetchone()
        return dict(row) if row else None


def get_resume(candidate_id, resume_id):
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM resumes WHERE id=? AND candidate_id=?""",
            (resume_id, candidate_id),
        ).fetchone()
        return dict(row) if row else None


# ---- outreach ----
def save_outreach(candidate_id, channel, subject, body, status="draft"):
    with _conn() as connection:
        return _insert_id(
            connection,
            """INSERT INTO outreach(candidate_id,channel,subject,body,status,created)
               VALUES(?,?,?,?,?,?)""",
            (candidate_id, channel, subject, body, status, time.time()),
        )


def list_outreach(candidate_id):
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM outreach WHERE candidate_id=? ORDER BY created DESC",
            (candidate_id,),
        )]


def mark_outreach(outreach_id, status):
    with _conn() as connection:
        cursor = connection.execute(
            "UPDATE outreach SET status=? WHERE id=?", (status, outreach_id)
        )
        return cursor.rowcount > 0


# ---- do-not-contact ----
def add_dnc(value, reason=""):
    with _conn() as connection:
        connection.execute(
            """INSERT INTO dnc(value,reason,created) VALUES(?,?,?)
               ON CONFLICT(value) DO NOTHING""",
            (value.strip().lower(), reason, time.time()),
        )


def is_dnc(value):
    if not value:
        return False
    with _conn() as connection:
        return connection.execute(
            "SELECT 1 FROM dnc WHERE value=?", (value.strip().lower(),)
        ).fetchone() is not None


def list_dnc():
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM dnc ORDER BY created DESC"
        )]


def stats():
    with _conn() as connection:
        total = _scalar(connection.execute("SELECT COUNT(*) FROM candidates"))
        by_stage = {stage: 0 for stage in PIPELINE_STAGES}
        for row in connection.execute(
            "SELECT stage,COUNT(*) AS count FROM candidates GROUP BY stage"
        ):
            by_stage[row["stage"]] = row["count"]
        enriched = _scalar(connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE enrich_status='success'"
        ))
        jobs = _scalar(connection.execute("SELECT COUNT(*) FROM jobs"))
        dnc = _scalar(connection.execute("SELECT COUNT(*) FROM dnc"))
        return {
            "total_candidates": total,
            "by_stage": by_stage,
            "enriched": enriched,
            "jobs": jobs,
            "dnc": dnc,
            "database": backend_name(),
        }


def reset():
    """Wipe the SQLite test/demo database. Production PostgreSQL reset is blocked."""
    if config.DATABASE_URL:
        raise RuntimeError("Refusing to reset a configured PostgreSQL database.")
    path = Path(config.DB_PATH)
    if path.exists():
        path.unlink()
