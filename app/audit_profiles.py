"""Full-board audit: re-verify EVERY profile against its own résumé.

Unlike clean_names_llm (which only salvages hidden junk), this re-reads the
actual résumé for every profile and corrects name / specialty / city / state /
zip from what the document really says. It is deliberately conservative:

  • salvaged   — profile was hidden junk, résumé yields a real name → fix + show
  • verified   — shown name agrees with the résumé → apply field corrections
  • conflict   — résumé shows a DIFFERENT real person → HIDE + log, never overwrite
  • unconfirmed— résumé readable but no real name found → keep as-is, log for review
  • no_text    — résumé unreadable (e.g. scanned image) → keep as-is, log for review

So the board only ever shows data confirmed against the résumé; anything the
model is unsure about is flagged in a CSV for a human, not silently changed.

Budget-capped: tracks real token usage and stops before spend crosses --budget
(default $20). Resumable: every processed profile is recorded in a manifest, so
re-running continues where it stopped and never re-pays for done work.

    python -m app.audit_profiles                       # full audit, $20 cap
    python -m app.audit_profiles --budget 10           # stop at ~$10
    python -m app.audit_profiles --limit 200           # try a small slice first
    python -m app.audit_profiles --dry-run             # read+LLM, write nothing

Outputs (next to the project root, override with --manifest / --review):
    audit_manifest.jsonl   resume log — one line per processed profile
    audit_review.csv       the flagged rows a human should eyeball
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from .clean_names_llm import (_apply, _llm, _resume_text_from_url, _title,
                              cost_usd)
from .config import settings
from .database import SessionLocal
from .importers.parsing import classify_provider, is_real_name
from .models import Profile

_COLS = (
    Profile.profile_id, Profile.resume_url, Profile.first_name, Profile.last_name,
    Profile.headline, Profile.bio, Profile.specialty, Profile.profession_type,
    Profile.american_board, Profile.provider_category, Profile.city,
    Profile.state_code, Profile.is_listable,
)
_REVIEW_COLS = ["profile_id", "outcome", "was_listable", "old_first", "old_last",
                "llm_first", "llm_last", "old_specialty", "llm_specialty",
                "old_city", "llm_city", "resume_url"]


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _agree(nf: str, nl: str, of: str | None, ol: str | None) -> bool:
    """Does the résumé's name agree with the existing name? Conservative — only
    a clear last-name mismatch (with different first name) counts as a conflict."""
    nln, oln = _norm(nl), _norm(ol)
    if not oln or not nln:
        return True                       # nothing to compare → not a conflict
    if nln == oln or nln in oln or oln in nln:
        return True
    return _norm(nf) == _norm(of) and bool(_norm(nf))


def _load_done(path: Path) -> set[str]:
    done: set[str] = set()
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["profile_id"])
                except Exception:  # noqa: BLE001
                    continue
    return done


def _search_text(row: dict, first, last, specialty, city, state, category) -> str:
    tmp = Profile(
        first_name=first, last_name=last, headline=row["headline"], bio=row["bio"],
        specialty=specialty, profession_type=row["profession_type"], city=city,
        state_code=state, american_board=row["american_board"],
        provider_category=category)
    tmp.rebuild_search_text()
    return tmp.search_text


def _fetch(row: dict):
    """The slow, network-bound part (résumé download + LLM), run per worker
    thread. No DB access. Returns (row, "NO_TEXT" | raw-dict | None)."""
    text = _resume_text_from_url(row["resume_url"])
    if not text:
        return row, "NO_TEXT"
    return row, _llm(text)


def run(*, budget: float = 20.0, limit: int | None = None, dry_run: bool = False,
        manifest: str = "audit_manifest.jsonl", review: str = "audit_review.csv",
        workers: int = 8, flush_every: int = 25) -> dict:
    manifest_path, review_path = Path(manifest), Path(review)
    done = _load_done(manifest_path)
    stats = {k: 0 for k in ("processed", "salvaged", "verified", "conflict",
                            "unconfirmed", "no_text", "llm_error", "still_junk")}

    # Phase 1 — load the work list (hidden/junk first so the budget fixes the
    # worst data first), skip anything already done, then release the connection.
    db = SessionLocal()
    try:
        q = (select(*_COLS).where(Profile.resume_url.isnot(None))
             .order_by(Profile.is_listable, Profile.created_at))
        if limit:
            q = q.limit(limit)
        rows = [dict(r._mapping) for r in db.execute(q)]
    finally:
        db.close()
    rows = [r for r in rows if r["profile_id"] not in done]

    print(f"Auditing up to {len(rows)} profile(s) with {settings.llm_model}, "
          f"budget ${budget:.2f}{' (DRY RUN)' if dry_run else ''} …\n")

    pending: list[dict] = []          # DB updates to flush
    manifest_batch: list[dict] = []   # resume records to append
    review_batch: list[dict] = []     # flagged rows to append
    review_new = not review_path.exists()

    def flush():
        nonlocal pending, manifest_batch, review_batch, review_new
        if pending and not dry_run:
            _apply(pending)
        if manifest_batch and not dry_run:
            with open(manifest_path, "a", encoding="utf-8") as f:
                for rec in manifest_batch:
                    f.write(json.dumps(rec) + "\n")
        if review_batch:
            with open(review_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_REVIEW_COLS)
                if review_new:
                    w.writeheader()
                    review_new = False
                for rec in review_batch:
                    w.writerow(rec)
        pending, manifest_batch, review_batch = [], [], []

    def flag(row, outcome, raw):
        review_batch.append({
            "profile_id": row["profile_id"], "outcome": outcome,
            "was_listable": row["is_listable"],
            "old_first": row["first_name"], "old_last": row["last_name"],
            "llm_first": (raw or {}).get("first_name"),
            "llm_last": (raw or {}).get("last_name"),
            "old_specialty": row["specialty"],
            "llm_specialty": (raw or {}).get("specialty"),
            "old_city": row["city"], "llm_city": (raw or {}).get("city"),
            "resume_url": row["resume_url"],
        })

    # LLM/résumé fetches run concurrently (network-bound); classification and all
    # DB / file writes stay on this single thread, so they need no locking. Budget
    # is checked between chunks — worst-case overshoot is one chunk (cents).
    chunk = max(workers * 2, workers)
    stopped = False
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for start in range(0, len(rows), chunk):
            if cost_usd() >= budget:
                print(f"\nBudget ${budget:.2f} reached (est ${cost_usd():.2f}). Stopping — "
                      f"re-run later to continue where it left off.")
                stopped = True
                break
            for row, raw in ex.map(_fetch, rows[start:start + chunk]):
                stats["processed"] += 1
                pid = row["profile_id"]
                if raw == "NO_TEXT":
                    stats["no_text"] += 1
                    flag(row, "no_text", None)
                    manifest_batch.append({"profile_id": pid, "outcome": "no_text"})
                else:
                    outcome = _decide_and_stage(row, raw, stats, pending, flag)
                    manifest_batch.append({"profile_id": pid, "outcome": outcome})
            flush()
            print(f"  … {stats['processed']}/{len(rows)}  "
                  f"salvaged={stats['salvaged']} verified={stats['verified']} "
                  f"conflict={stats['conflict']} est=${cost_usd():.2f}")

    flush()
    print(f"\nDone: {stats}")
    print(f"LLM: {_usage_line()}  est cost ${cost_usd():.4f}")
    if stats["conflict"] or stats["unconfirmed"] or stats["no_text"] or stats["llm_error"]:
        print(f"Flagged for review -> {review_path}")
    return stats


def _decide_and_stage(row: dict, raw, stats: dict, pending: list, flag) -> str:
    """Classify one profile and stage its DB update / review flag. Returns outcome."""
    if raw is None:
        stats["llm_error"] += 1
        flag(row, "llm_error", None)
        return "llm_error"

    first, last = _title(raw.get("first_name")), _title(raw.get("last_name"))
    llm_real = bool(first and last and is_real_name(first, last))
    existing_real = is_real_name(row["first_name"], row["last_name"])

    if not llm_real:
        if existing_real:                       # shown name unconfirmed by résumé
            stats["unconfirmed"] += 1
            flag(row, "unconfirmed", raw)
            return "unconfirmed"
        stats["still_junk"] += 1                # was hidden, stays hidden
        return "still_junk"

    if existing_real and not _agree(first, last, row["first_name"], row["last_name"]):
        stats["conflict"] += 1                  # résumé is a different person
        pending.append({"profile_id": row["profile_id"], "is_listable": False})
        flag(row, "conflict", raw)
        return "conflict"

    # Confident: apply the résumé's values and make sure the profile is shown.
    specialty = (_title(raw["specialty"]) if raw.get("specialty") else None) or row["specialty"]
    city = (_title(raw["city"]) if raw.get("city") else None) or row["city"]
    st = str(raw.get("state") or "").strip().upper()
    state = st if len(st) == 2 else row["state_code"]
    category = classify_provider(row["profession_type"], specialty, row["headline"]) \
        or row["provider_category"]
    upd = {
        "profile_id": row["profile_id"],
        "first_name": first[:100], "last_name": last[:100],
        "specialty": specialty[:100] if specialty else None,
        "city": city[:120] if city else None,
        "state_code": state,
        "provider_category": category,
        "is_listable": True,
        "search_text": _search_text(row, first, last, specialty, city, state, category),
    }
    z = str(raw.get("zip") or "").strip()
    if re.fullmatch(r"\d{5}", z):
        upd["zip_code"] = z
    pending.append(upd)
    outcome = "salvaged" if not existing_real else "verified"
    stats[outcome] += 1
    return outcome


def _usage_line() -> str:
    from .clean_names_llm import _USAGE
    return (f"{_USAGE['calls']} calls, {_USAGE['in']:,} in + "
            f"{_USAGE['out']:,} out tokens")


def main() -> int:
    ap = argparse.ArgumentParser(description="Full-board résumé audit (budget-capped)")
    ap.add_argument("--budget", type=float, default=20.0, help="max USD to spend (default 20)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N profiles")
    ap.add_argument("--dry-run", action="store_true", help="read+LLM, write nothing")
    ap.add_argument("--workers", type=int, default=8, help="concurrent LLM calls (default 8)")
    ap.add_argument("--manifest", default="audit_manifest.jsonl", help="resume log path")
    ap.add_argument("--review", default="audit_review.csv", help="flagged-rows CSV path")
    args = ap.parse_args()
    if not (settings.llm_enabled and settings.llm_api_key and settings.llm_model):
        print("ERROR: LLM not configured (LLM_ENABLED / GEMINI_API_KEY / LLM_MODEL).")
        return 1
    run(budget=args.budget, limit=args.limit, dry_run=args.dry_run,
        workers=args.workers, manifest=args.manifest, review=args.review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
