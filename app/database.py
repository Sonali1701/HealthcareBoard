"""SQLAlchemy engine, session factory and declarative base.

Written to stay portable between SQLite (local dev) and PostgreSQL (prod):
 - UUIDs are stored as 36-char strings.
 - Arrays / JSONB map to SQLAlchemy JSON.
 - Geo points are plain lat/lng floats (no PostGIS dependency).
 - Full-text search uses a denormalised lowercase ``search_text`` column + LIKE.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import DateTime, String, TypeDecorator, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column, sessionmaker

from .config import settings

logger = logging.getLogger("healthboard.database")

_is_sqlite = settings.database_url.startswith("sqlite")

if _is_sqlite:
    # SQLite needs check_same_thread=False for FastAPI's threadpool usage.
    connect_args = {"check_same_thread": False}
else:
    # Neon's serverless compute drops idle connections when it autosuspends.
    # TCP keepalives make the OS notice a dead socket quickly, and a short
    # connect_timeout stops a cold start from hanging a request forever.
    connect_args = {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    echo=False,
    future=True,
    # For Neon/Postgres: ping a connection before every use and reconnect
    # transparently if it was dropped, and recycle well before Neon's ~5-min
    # idle timeout so we never hand out a stale socket. Ignored for SQLite.
    **({} if _is_sqlite else {
        "pool_pre_ping": True,
        "pool_recycle": 180,
        "pool_size": 5,
        "max_overflow": 10,
    }),
)

SessionLocal = sessionmaker(
    bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
)


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_connection, connection_record):  # noqa: ANN001
    """Enforce foreign keys on SQLite (off by default)."""
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """Timezone-aware UTC datetime that is portable across SQLite and Postgres.

    SQLite drops tzinfo on storage, so naive values come back and break
    comparisons with aware datetimes. This decorator normalises everything to
    UTC on the way in and re-attaches UTC tzinfo on the way out, so the rest of
    the app always works with aware UTC datetimes.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):  # noqa: ANN001
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base with common timestamp helpers."""


# Reusable column factories ------------------------------------------------

def uuid_pk():
    return mapped_column(String(36), primary_key=True, default=new_uuid)


def uuid_fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE"):
    from sqlalchemy import ForeignKey

    return mapped_column(
        String(36), ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


def created_col():
    return mapped_column(TZDateTime, default=utcnow, nullable=False)


def updated_col():
    return mapped_column(
        TZDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# FastAPI dependency -------------------------------------------------------

def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema() -> list[str]:
    """Additively bring the live schema up to the models — the deploy-time
    migration step, run automatically on every boot.

    ``create_all()`` adds missing TABLES but never missing COLUMNS on a table
    that already exists. That gap is why every feature so far shipped with a
    hand-written ``app/migrate_*.py`` that had to be run against the database
    before the code that needed the column went live — miss it and the deploy
    500s. This closes the gap: after create_all, any *optional* (nullable)
    column the models declare but the database lacks is added in place.

    Deliberately conservative and safe on the populated production database:
      * additive only — it never drops or retypes a column;
      * a missing NOT NULL column with no server default can't be added to a
        table that already has rows, so those are logged for a real migration
        (with a backfill) instead of being guessed at;
      * every ALTER is isolated, so one problem column can't stop startup.

    Returns the list of columns it added (empty when the schema is already in
    sync, which is the normal case).
    """
    from . import models  # noqa: F401 — register every table on Base.metadata

    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    is_pg = engine.dialect.name == "postgresql"
    added: list[str] = []
    needs_manual: list[str] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table — create_all already made it in full
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            # Can't add a required column to a table that already has rows
            # unless the DB itself can fill it — leave that to a real migration.
            if not col.nullable and col.server_default is None:
                needs_manual.append(f"{table.name}.{col.name}")
                continue
            coltype = col.type.compile(dialect=engine.dialect)
            exists_guard = "IF NOT EXISTS " if is_pg else ""
            ddl = (f'ALTER TABLE "{table.name}" '
                   f'ADD COLUMN {exists_guard}"{col.name}" {coltype}')
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
            except Exception as exc:  # noqa: BLE001 — never let one column stop boot
                logger.warning("ensure_schema: could not add %s.%s: %s",
                               table.name, col.name, exc)

    if added:
        logger.info("ensure_schema: added missing columns: %s", ", ".join(added))
    if needs_manual:
        logger.warning(
            "ensure_schema: these NOT NULL columns need a manual migration with a "
            "backfill (the code expects them but the DB lacks them): %s",
            ", ".join(needs_manual))
    return added


def init_db() -> None:
    """Create all tables and additively sync any missing columns.

    Imports models so they register on Base.metadata, creates missing tables,
    then runs ensure_schema() so a deploy can't 500 on a column the code expects
    but the database hasn't got yet.
    """
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_schema()
