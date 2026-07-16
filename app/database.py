"""SQLAlchemy engine, session factory and declarative base.

Written to stay portable between SQLite (local dev) and PostgreSQL (prod):
 - UUIDs are stored as 36-char strings.
 - Arrays / JSONB map to SQLAlchemy JSON.
 - Geo points are plain lat/lng floats (no PostGIS dependency).
 - Full-text search uses a denormalised lowercase ``search_text`` column + LIKE.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import DateTime, String, TypeDecorator, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, mapped_column, sessionmaker

from .config import settings

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


def init_db() -> None:
    """Create all tables. Imports models so they register on Base.metadata."""
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
