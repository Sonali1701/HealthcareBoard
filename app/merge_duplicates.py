"""Fold duplicate profiles into one record each, across the whole directory.

A duplicate here means the SAME phone number AND the same name. Neither signal
is safe alone: one phone number is shared by 23 unrelated candidates who all
went through the same agency switchboard, and name+city groups together
everyone whose "name" the parser lifted off a location line.

The work is done in SQL. Grouping ~108k profiles by a normalised phone number
cannot use an index, so pulling candidates into Python meant a full scan plus
thousands of round trips to a remote database; one windowed UPDATE does it in
a single pass.

The richest record survives (completeness, then contact detail, then a résumé,
then experience, then age). Everything else is hidden and stamped with
`merged_into` — nothing is deleted, and `--restore` puts it all back.

Run:  python -m app.merge_duplicates --dry-run
      python -m app.merge_duplicates
      python -m app.merge_duplicates --restore
"""
from __future__ import annotations

import argparse

from sqlalchemy import text

from .database import SessionLocal

MERGE_REASON = "merged_duplicate"

# Members of a duplicate group, ranked so rank 1 is the record worth keeping.
_RANKED = """
    SELECT profile_id,
           row_number() OVER w  AS rn,
           first_value(profile_id) OVER w AS keeper
      FROM profiles
     WHERE is_listable IS TRUE
       AND merged_into IS NULL
       AND phone IS NOT NULL
       AND length(regexp_replace(phone, '[^0-9]', '', 'g')) >= 10
       AND first_name IS NOT NULL AND btrim(first_name) <> ''
       AND last_name  IS NOT NULL AND btrim(last_name)  <> ''
    WINDOW w AS (
        PARTITION BY right(regexp_replace(phone, '[^0-9]', '', 'g'), 10),
                     lower(btrim(first_name)), lower(btrim(last_name))
            ORDER BY coalesce(completion_score, 0) DESC,
                     (email IS NOT NULL AND btrim(email) <> '') DESC,
                     (resume_url IS NOT NULL) DESC,
                     coalesce(years_experience, 0) DESC,
                     created_at ASC
    )
"""

_COUNT_DUPES = f"SELECT count(*) FROM ({_RANKED}) r WHERE r.rn > 1"
_COUNT_GROUPS = f"SELECT count(DISTINCT r.keeper) FROM ({_RANKED}) r WHERE r.rn > 1"

_MERGE = f"""
UPDATE profiles p
   SET is_listable   = FALSE,
       merged_into   = r.keeper,
       merged_at     = now(),
       screen_reason = '{MERGE_REASON}'
  FROM ({_RANKED}) r
 WHERE p.profile_id = r.profile_id
   AND r.rn > 1
"""

# Anything shortlisted under a folded-in record follows its survivor. The
# DELETE first clears rows that would collide with the survivor's own entry.
_POOLS_DEDUPE = """
DELETE FROM talent_pool_members m
 USING profiles p
 WHERE m.profile_id = p.profile_id
   AND p.merged_into IS NOT NULL
   AND EXISTS (SELECT 1 FROM talent_pool_members k
                WHERE k.pool_id = m.pool_id AND k.profile_id = p.merged_into)
"""
_POOLS_MOVE = """
UPDATE talent_pool_members m
   SET profile_id = p.merged_into
  FROM profiles p
 WHERE m.profile_id = p.profile_id
   AND p.merged_into IS NOT NULL
"""


def run(dry_run: bool = False) -> None:
    db = SessionLocal()
    try:
        dupes = db.scalar(text(_COUNT_DUPES)) or 0
        groups = db.scalar(text(_COUNT_GROUPS)) or 0
        print(f"{groups:,} duplicate groups · {dupes:,} records to fold in")
        if not dupes:
            return
        if dry_run:
            print("DRY RUN - nothing written")
            return

        merged = db.execute(text(_MERGE)).rowcount
        moved_out = db.execute(text(_POOLS_DEDUPE)).rowcount
        moved = db.execute(text(_POOLS_MOVE)).rowcount
        db.commit()

        print(f"folded in {merged:,} duplicate records")
        if moved or moved_out:
            print(f"  pool memberships moved to survivors: {moved:,} "
                  f"({moved_out:,} collided and were dropped)")
        print(f"duplicate groups remaining: {db.scalar(text(_COUNT_GROUPS)) or 0:,}")
        print("directory now lists "
              f"{db.scalar(text('SELECT count(*) FROM profiles WHERE is_listable IS TRUE')):,}")
    finally:
        db.close()


def restore() -> None:
    db = SessionLocal()
    try:
        n = db.execute(text(
            "UPDATE profiles SET is_listable = TRUE, merged_into = NULL, "
            "merged_at = NULL, screen_reason = NULL WHERE screen_reason = :r"),
            {"r": MERGE_REASON}).rowcount
        db.commit()
        print(f"restored {n:,} merged profiles")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()
    restore() if a.restore else run(dry_run=a.dry_run)
