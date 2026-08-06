"""One command that keeps the directory clean while imports keep arriving.

New résumés land continuously, and each arrival needs the same three passes:
screen out anything that is not healthcare, put the person on the map, and fold
in any duplicate of someone already listed. Every pass is incremental — it only
touches rows it has not already handled — so this is safe to run on a loop.

Run once:      python -m app.maintain_directory
Run forever:   python -m app.maintain_directory --loop 30      (every 30 min)
Skip the model: add --no-llm
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

from sqlalchemy import text

from .database import SessionLocal


def _counts() -> dict:
    db = SessionLocal()
    try:
        q = lambda s: db.execute(text(s)).scalar() or 0  # noqa: E731
        return {
            "listable": q("SELECT count(*) FROM profiles WHERE is_listable IS TRUE"),
            # What THIS tool will act on: profiles carrying no role evidence, in
            # either scope. Profiles that already have a specialty or parsed
            # skills are assumed genuine and are not re-read — see `unchecked`.
            "queued": q("""
                SELECT count(*) FROM profiles p
                 WHERE p.is_listable IS TRUE AND p.resume_url IS NOT NULL
                   AND p.screen_reason IS NULL
                   AND coalesce(p.specialty, '') = ''
                   AND ((p.provider_category IN ('Others') OR p.provider_category IS NULL)
                        OR NOT EXISTS (SELECT 1 FROM profile_skills s
                                        WHERE s.profile_id = p.profile_id))"""),
            # Listed but never content-checked, because they looked credible.
            # Sampling put roughly 12% junk in this population too, so it is a
            # known remaining gap rather than a clean bill of health.
            "unchecked": q("""
                SELECT count(*) FROM profiles
                 WHERE is_listable IS TRUE AND screen_reason IS NULL"""),
            "ungeocoded": q("""
                SELECT count(*) FROM profiles
                 WHERE lat IS NULL AND (zip_code IS NOT NULL OR city IS NOT NULL)"""),
        }
    finally:
        db.close()


def once(use_llm: bool = True, workers: int = 4) -> None:
    from . import backfill_geocodes, merge_duplicates, screen_directory, screen_junk_names

    stamp = datetime.now().strftime("%H:%M:%S")
    before = _counts()
    print(f"[{stamp}] listable={before['listable']:,} "
          f"queued={before['queued']:,} ungeocoded={before['ungeocoded']:,} "
          f"(never content-checked: {before['unchecked']:,})")

    # 1. Names first — it reads columns only, so it clears the cheap cases
    #    before anything downloads a résumé.
    print("  names…", flush=True)
    screen_junk_names.run()

    # 2. Content. Both scopes, because a bad résumé can arrive with or without
    #    a clinical category attached.
    for scope in ("others", "clinical"):
        print(f"  content ({scope})…", flush=True)
        screen_directory.run(workers=workers, scope=scope, use_llm=use_llm)

    # 3. Map the new arrivals — rows are inserted with a NULL position, so
    #    radius search cannot see them until this runs.
    print("  geocoding…", flush=True)
    backfill_geocodes.run()

    # 4. Fold in anyone already in the directory under another record.
    print("  duplicates…", flush=True)
    merge_duplicates.run()

    after = _counts()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] done — "
          f"listable={after['listable']:,} "
          f"({after['listable'] - before['listable']:+,}) "
          f"queued={after['queued']:,}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="MINUTES",
                    help="keep running, waiting this many minutes between passes")
    ap.add_argument("--no-llm", action="store_true",
                    help="keyword screening only, no model calls")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    while True:
        try:
            once(use_llm=not a.no_llm, workers=a.workers)
        except Exception as exc:                    # a bad pass must not end the loop
            print(f"pass failed: {type(exc).__name__}: {exc}", flush=True)
        if not a.loop:
            break
        print(f"sleeping {a.loop} min\n", flush=True)
        time.sleep(a.loop * 60)
