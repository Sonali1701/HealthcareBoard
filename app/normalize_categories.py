"""Fold the stray "Other" provider category into "Others" (idempotent).

The directory lists only the five canonical categories, so profiles that were
imported as the singular "Other" were invisible in every tab AND were never
picked up by the screening sweep, which targets "Others"/NULL. One typo-level
value, two silent failures.

Run:  python -m app.normalize_categories [--dry-run]
"""
from __future__ import annotations

import argparse

from sqlalchemy import func, select, update

from .database import SessionLocal
from .models import Profile

CANONICAL = "Others"
ALIASES = ("Other", "other", "OTHER", "others")


def run(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Profile.provider_category, func.count())
            .where(Profile.provider_category.in_(ALIASES))
            .group_by(Profile.provider_category)
        ).all()
        if not rows:
            print("nothing to normalise")
            return
        for value, n in rows:
            print(f"  {value!r}: {n:,}")

        n = db.execute(
            update(Profile)
            .where(Profile.provider_category.in_(ALIASES))
            .values(provider_category=CANONICAL)
        ).rowcount
        if dry_run:
            db.rollback()
            print(f"DRY RUN - would move {n:,} profiles to {CANONICAL!r}")
            return
        db.commit()
        print(f"moved {n:,} profiles to {CANONICAL!r}")

        after = db.execute(
            select(Profile.provider_category, func.count())
            .where(Profile.is_listable.is_(True))
            .group_by(Profile.provider_category).order_by(func.count().desc())
        ).all()
        print("\nlistable by category now:")
        for c, cnt in after:
            print(f"  {str(c):12s} {cnt:,}")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
