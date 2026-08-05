"""Add profiles.resume_sections — LLM-extracted résumé sections.

The résumé viewer re-parses the source file with regex heuristics on every open,
which breaks on PDFs that lost their spacing or use multi-column layouts. This
column stores clean sections extracted once by an LLM; the viewer prefers it and
falls back to the parser when it is empty.

    python -m app.migrate_resume_sections            # add the column
    python -m app.migrate_resume_sections --report   # how many are populated

Safe to re-run. Populate the column with:
    python -m app.extract_resume_sections --limit 50
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from .database import SessionLocal, engine


def _add_column() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        if dialect == "postgresql":
            conn.execute(text(
                "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS resume_sections JSON"))
        else:
            cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
            if "resume_sections" not in cols:
                conn.execute(text(
                    "ALTER TABLE profiles ADD COLUMN resume_sections JSON"))
    print("Column resume_sections ensured.")


def _report() -> None:
    db = SessionLocal()
    try:
        total, with_resume, extracted = db.execute(text(
            "SELECT COUNT(*),"
            "       COUNT(resume_url),"
            "       COUNT(resume_sections)"
            "  FROM profiles")).one()
        print(f"profiles            : {total:,}")
        print(f"  with a résumé file: {with_resume:,}")
        print(f"  sections extracted: {extracted:,}")
        remaining = (with_resume or 0) - (extracted or 0)
        print(f"  still to extract  : {remaining:,}")
    finally:
        db.close()


def main() -> None:
    if "--report" in sys.argv:
        _report()
        return
    _add_column()
    _report()
    print("Done.")


if __name__ == "__main__":
    main()
