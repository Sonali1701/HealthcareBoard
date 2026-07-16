"""Add a DB-level uniqueness backstop so duplicate profiles can't be inserted.

Dedup logic in the importer only helps if the importer runs it. This puts the
guard in the *database*: it physically refuses a second profile whose email
already exists — no matter what inserts it (the API, a future importer, or an
old/standalone copy of data_upload.py). It cannot drift, because it isn't code.

Covers the EMAIL key only — the strongest, most common identity signal. The
name+phone key stays in application code (too fuzzy for a hard DB constraint).
Empty/NULL emails are excluded, so contact-less profiles are unaffected.

    python -m app.migrate_dedup_index      # clear residual email dupes + build index

Idempotent and fully reversible:  DROP INDEX ux_profiles_email_ci;
"""
from __future__ import annotations

from sqlalchemy import text

from .database import SessionLocal, engine
from .dedup_profiles import analyze

INDEX_NAME = "ux_profiles_email_ci"
_EMAIL_DUP_SQL = """
    SELECT count(*) FROM (
        SELECT lower(email) e FROM profiles
        WHERE email IS NOT NULL AND email <> ''
        GROUP BY lower(email) HAVING count(*) > 1
    ) g"""


def _email_dupes(db) -> int:
    return db.execute(text(_EMAIL_DUP_SQL)).scalar() or 0


def _clear_email_dupes() -> int:
    """Delete unowned/inactive duplicate profiles until no email collisions remain.

    Returns the count of *unresolvable* collisions (both sides protected), which
    would block the unique index and need manual review.
    """
    for _ in range(12):
        db = SessionLocal()
        try:
            remaining = _email_dupes(db)
            if remaining == 0:
                return 0
            ids = analyze(db)["delete_ids"]
            if not ids:
                # collisions remain but every row is protected (registered user
                # or has activity) — can't auto-resolve.
                return remaining
            for i in range(0, len(ids), 500):
                db.execute(text("DELETE FROM profiles WHERE profile_id = ANY(:ids)"),
                           {"ids": ids[i:i + 500]})
                db.commit()
            print(f"  cleared {len(ids)} duplicate(s); rechecking …")
        finally:
            db.close()
    return _email_dupes(SessionLocal())


def _build_index() -> None:
    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON profiles (lower(email)) WHERE email IS NOT NULL AND email <> ''"))
    print(f"Unique index {INDEX_NAME} ensured.")


def main() -> int:
    print("Clearing any residual duplicate emails …")
    blocked = _clear_email_dupes()
    if blocked:
        print(f"WARNING: {blocked} email(s) still shared by protected profiles "
              "(registered users / have activity). Resolve manually, then re-run.")
        return 1
    # Build with one retry in case a live import inserts a dup mid-build.
    for attempt in range(3):
        try:
            _build_index()
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Index build failed ({str(exc)[:120]}); clearing and retrying …")
            _clear_email_dupes()
    print("ERROR: could not build the unique index (concurrent inserts?).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
