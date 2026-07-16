"""Find and remove duplicate provider profiles.

The résumé importer only de-dupes by file content + NPI, so the same person
uploaded as a different file (PDF vs DOCX, an updated résumé, a second copy)
becomes a *second* profile in the directory. This collapses those.

Two profiles are the SAME person when they share:
  - the same email address, OR
  - the same last name + phone number (last 10 digits)

Within each duplicate cluster we keep ONE winner (the most complete) and delete
the rest. Deletion cascades to the loser's owned child rows (skills, licenses,
experience, …) via the CASCADE foreign keys.

Two hard safety rules — a profile is NEVER deleted if it:
  - belongs to a real registered user (user_id IS NOT NULL), or
  - has real activity (an application or a saved job).
Such a profile can still be the *winner* of its cluster; it just can't be a loser.

    python -m app.dedup_profiles                 # dry-run report (writes nothing)
    python -m app.dedup_profiles --sample 40     # report + show 40 example clusters
    python -m app.dedup_profiles --apply          # actually delete the duplicates

Safe to re-run. After a big run, the directory counts drop by the number deleted.
"""
from __future__ import annotations

import argparse
import re
import sys

from sqlalchemy import text

from .database import SessionLocal


def _norm_email(v) -> str | None:
    v = (v or "").strip().lower()
    return v if v and "@" in v else None


def _norm_phone(v) -> str | None:
    digits = re.sub(r"\D", "", v or "")
    return digits[-10:] if len(digits) >= 10 else None


class _UF:
    """Tiny union-find over integer row indexes."""

    def __init__(self, n: int) -> None:
        self.p = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:      # path compression
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _winner_key(row: dict):
    """Higher tuple = better kept profile."""
    return (
        1 if row["user_id"] else 0,          # registered account wins
        1 if row["is_listable"] else 0,      # visible over hidden
        row["completion"] or 0,              # most complete
        1 if row["resume_url"] else 0,       # has résumé on file
        1 if row["npi"] else 0,              # has NPI
        row["created_at"],                   # newest as final tiebreak
    )


def analyze(db):
    rows = db.execute(text(
        "SELECT profile_id, user_id, is_listable, completion_score, resume_url, "
        "npi_number, created_at, first_name, last_name, email, phone "
        "FROM profiles")).mappings().all()
    rows = [dict(r) for r in rows]
    for r in rows:
        r["completion"] = r.pop("completion_score", None)
        r["npi"] = r.pop("npi_number", None)

    # Profiles that must never be deleted as a loser (real activity).
    protected = set()
    for tbl, col in (("applications", "profile_id"), ("saved_jobs", "profile_id")):
        for (pid,) in db.execute(text(f"SELECT DISTINCT {col} FROM {tbl}")):
            protected.add(pid)

    uf = _UF(len(rows))
    email_first: dict[str, int] = {}
    phone_first: dict[tuple, int] = {}
    for i, r in enumerate(rows):
        em = _norm_email(r["email"])
        if em is not None:
            if em in email_first:
                uf.union(email_first[em], i)
            else:
                email_first[em] = i
        ph = _norm_phone(r["phone"])
        last = (r["last_name"] or "").strip().lower()
        if ph is not None and last:
            k = (last, ph)
            if k in phone_first:
                uf.union(phone_first[k], i)
            else:
                phone_first[k] = i

    clusters: dict[int, list[int]] = {}
    for i in range(len(rows)):
        clusters.setdefault(uf.find(i), []).append(i)

    dup_clusters = []
    delete_ids: list[str] = []
    skipped_protected = 0
    for members in clusters.values():
        if len(members) < 2:
            continue
        group = [rows[i] for i in members]
        winner = max(group, key=_winner_key)
        losers = []
        for r in group:
            if r["profile_id"] == winner["profile_id"]:
                continue
            if r["user_id"] or r["profile_id"] in protected:
                skipped_protected += 1
                continue
            losers.append(r)
        if losers:
            dup_clusters.append({"winner": winner, "losers": losers})
            delete_ids.extend(r["profile_id"] for r in losers)

    return {
        "total": len(rows),
        "clusters": dup_clusters,
        "delete_ids": delete_ids,
        "skipped_protected": skipped_protected,
    }


def _fmt(r: dict) -> str:
    name = f"{r['first_name'] or ''} {r['last_name'] or ''}".strip() or "(no name)"
    tag = "listed" if r["is_listable"] else "hidden"
    who = "USER" if r["user_id"] else "import"
    return (f"{name:28.28s} | {tag:6s} | {who:6s} | "
            f"score={r['completion'] or 0:>3} | {r['email'] or r['phone'] or '-'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="De-duplicate provider profiles")
    ap.add_argument("--apply", action="store_true", help="actually delete duplicates")
    ap.add_argument("--sample", type=int, default=15, help="how many example clusters to print")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        res = analyze(db)
        n_del = len(res["delete_ids"])
        n_clusters = len(res["clusters"])
        print(f"Scanned {res['total']:,} profiles.")
        print(f"Duplicate clusters: {n_clusters:,}")
        print(f"Profiles to delete: {n_del:,}")
        print(f"Protected (kept — registered user or has activity): {res['skipped_protected']:,}")

        for c in res["clusters"][:max(0, args.sample)]:
            print("\n  KEEP   " + _fmt(c["winner"]))
            for l in c["losers"]:
                print("  delete " + _fmt(l))

        if not args.apply:
            print(f"\nDry run — nothing deleted. Re-run with --apply to delete {n_del:,} profile(s).")
            return 0

        if not res["delete_ids"]:
            print("\nNothing to delete.")
            return 0

        print(f"\nDeleting {n_del:,} duplicate profile(s) …")
        ids = res["delete_ids"]
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            db.execute(
                text("DELETE FROM profiles WHERE profile_id = ANY(:ids)"),
                {"ids": batch})
            db.commit()
            print(f"  … {min(i + 500, len(ids)):,}/{len(ids):,} deleted")
        print(f"Done. Deleted {n_del:,} duplicate profile(s).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
