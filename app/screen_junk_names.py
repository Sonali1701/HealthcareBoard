"""Hide listable profiles whose "name" is a document artifact, not a person.

The résumé parser sometimes lifts a section heading or a location line instead
of the candidate's name — "Not Found", "Core Competencies", "San TX". A profile
you cannot put a name to is unusable for outreach, so it should not sit in the
directory.

This re-applies `is_real_name` to profiles already in the database. It reads
only columns, never storage, so it runs in seconds rather than hours.

Run:  python -m app.screen_junk_names --dry-run
      python -m app.screen_junk_names
      python -m app.screen_junk_names --restore
"""
from __future__ import annotations

import argparse
from collections import Counter

from sqlalchemy import select, text

from .database import SessionLocal, utcnow
from .importers.parsing import is_real_name
from .models import Profile

REASON = "junk_name"


def run(dry_run: bool = False, batch: int = 2000) -> None:
    db = SessionLocal()
    try:
        rows = db.execute(
            select(Profile.profile_id, Profile.first_name, Profile.last_name)
            .where(Profile.is_listable.is_(True))
        ).all()
        print(f"checking {len(rows):,} listable profiles")

        bad = [(pid, f"{(f or '').strip()} {(l or '').strip()}".strip())
               for pid, f, l in rows if not is_real_name(f, l)]
        print(f"{len(bad):,} have an unusable name")
        for name, n in Counter(n.lower() for _, n in bad).most_common(12):
            print(f"   {n:5d}  {name}")

        if dry_run:
            print("\nDRY RUN - nothing written")
            return
        if not bad:
            return

        ids = [pid for pid, _ in bad]
        for i in range(0, len(ids), batch):
            chunk = ids[i:i + batch]
            db.execute(
                text("UPDATE profiles SET is_listable = FALSE, screen_reason = :r, "
                     "screened_at = :t WHERE profile_id = ANY(:ids)"),
                {"r": REASON, "t": utcnow(), "ids": chunk},
            )
            db.commit()
            print(f"   hidden {min(i + batch, len(ids)):,}/{len(ids):,}", flush=True)

        left = db.scalar(text("SELECT count(*) FROM profiles WHERE is_listable IS TRUE"))
        print(f"\ndirectory now lists {left:,} profiles")
    finally:
        db.close()


def restore() -> None:
    db = SessionLocal()
    try:
        n = db.execute(
            text("UPDATE profiles SET is_listable = TRUE, screen_reason = NULL, "
                 "screened_at = NULL WHERE screen_reason = :r"), {"r": REASON}).rowcount
        db.commit()
        print(f"restored {n:,} profiles")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()
    restore() if a.restore else run(dry_run=a.dry_run)
