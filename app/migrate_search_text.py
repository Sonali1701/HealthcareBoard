"""Make full license names searchable on existing profiles.

Appends e.g. "registered nurse" to the search_text of every RN profile so that
searching the full name finds them (the old search_text only had the code "rn").

    python -m app.migrate_search_text

Set-based (one UPDATE per license) so it's fast even on millions of rows, and
idempotent — re-running won't duplicate the appended text.
"""
from __future__ import annotations

from sqlalchemy import text

from .database import engine
from .importers.parsing import LICENSE_FULL_NAMES


def main() -> None:
    total = 0
    with engine.begin() as conn:
        for code, full in LICENSE_FULL_NAMES.items():
            res = conn.execute(text("""
                UPDATE profiles
                SET search_text = COALESCE(search_text, '') || ' ' || :full
                WHERE UPPER(TRIM(profession_type)) = :code
                  AND (search_text IS NULL OR search_text NOT LIKE :like)
            """), {"code": code, "full": full.lower(), "like": f"%{full.lower()}%"})
            if res.rowcount:
                print(f"  {code:5} -> +'{full.lower()}' on {res.rowcount} profile(s)")
            total += res.rowcount
    print(f"Done. Updated {total} profile(s).")


if __name__ == "__main__":
    main()
