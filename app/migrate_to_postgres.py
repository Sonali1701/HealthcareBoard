"""Copy ALL data from the local SQLite database into your Postgres database.

Use this to lift an existing healthboard.db (with your candidates, jobs, users,
messages, etc.) into a fresh Postgres DB — instead of re-importing from source.

Steps:
  1. Put your Postgres URL in .env  ->  DATABASE_URL=postgresql://...
  2. Create the tables in Postgres:
         .venv\\Scripts\\python -m alembic upgrade head
  3. Copy the data:
         .venv\\Scripts\\python -m app.migrate_to_postgres

Options:
  --source sqlite:///./healthboard.db   (default — the DB to copy FROM)
  --wipe                                empty the Postgres tables first (re-run safe)
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, delete, insert, select

import app.models  # noqa: F401  (registers every table on Base.metadata)
from app.config import settings
from app.database import Base


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="sqlite:///./healthboard.db",
                    help="SQLAlchemy URL to copy FROM (default: local SQLite)")
    ap.add_argument("--wipe", action="store_true",
                    help="delete existing rows in the target before copying")
    args = ap.parse_args()

    target = settings.database_url
    if target.startswith("sqlite"):
        print("DATABASE_URL is still SQLite. Set it to your Postgres URL in .env first.")
        return 1

    print(f"FROM: {args.source}\n  TO: {target.split('@')[-1]}\n")
    src = create_engine(args.source)
    dst = create_engine(target)
    Base.metadata.create_all(dst)  # no-op if `alembic upgrade head` already ran

    tables = list(Base.metadata.sorted_tables)  # FK-dependency order
    total = 0
    with src.connect() as s, dst.begin() as d:
        if args.wipe:
            for t in reversed(tables):
                d.execute(delete(t))
        for t in tables:
            rows = [dict(r._mapping) for r in s.execute(select(t))]
            if rows:
                d.execute(insert(t), rows)
            total += len(rows)
            print(f"  {t.name:<26} {len(rows)}")
    print(f"\nDone — copied {total} rows into Postgres.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
