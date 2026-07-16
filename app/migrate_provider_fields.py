"""Add the Providers-feature columns to the profiles table and backfill them.

Adds: phone, email, american_board, provider_category   (idempotent).
Backfills provider_category (Physicians/Nursing/Allied/APP) from each profile's
profession/specialty, and american_board from its certifications.

Run:  python -m app.migrate_provider_fields
"""
from __future__ import annotations

from sqlalchemy import select, text

from .database import SessionLocal, engine
from .importers.parsing import classify_provider, primary_american_board
from .models import Certification, Profile

COLUMNS = {
    "phone": "VARCHAR(30)",
    "email": "VARCHAR(255)",
    "american_board": "VARCHAR(150)",
    "provider_category": "VARCHAR(20)",
}


def _add_columns() -> None:
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for name, coltype in COLUMNS.items():
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE profiles ADD COLUMN IF NOT EXISTS {name} {coltype}"))
            else:  # sqlite: no IF NOT EXISTS for columns — check pragma
                cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE profiles ADD COLUMN {name} {coltype}"))
    print(f"Columns ensured on 'profiles': {', '.join(COLUMNS)}")


def _backfill() -> None:
    db = SessionLocal()
    cat = brd = 0
    try:
        profiles = db.scalars(select(Profile)).all()
        for p in profiles:
            new_cat = classify_provider(p.profession_type, p.specialty, p.headline)
            if new_cat and new_cat != p.provider_category:
                p.provider_category = new_cat
                cat += 1
            if not p.american_board:
                names = [c.cert_name for c in
                         db.scalars(select(Certification).where(
                             Certification.profile_id == p.profile_id)).all()]
                board = primary_american_board(names)
                if board:
                    p.american_board = board
                    brd += 1
            p.rebuild_search_text()
        db.commit()
        print(f"Backfilled provider_category on {cat} profile(s), "
              f"american_board on {brd} profile(s), out of {len(profiles)}.")
    finally:
        db.close()


def main() -> None:
    _add_columns()
    _backfill()


if __name__ == "__main__":
    main()
