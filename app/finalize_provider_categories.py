"""Finalize every unclassified profile into the approved taxonomy or ``Others``.

The command is deliberately conservative:

* the four clinical categories require explicit résumé evidence;
* unmatched but valid profiles become ``Others``;
* existing hidden profiles are never automatically published;
* listable profiles with a junk identity, missing résumé, unreadable file, or
  effectively empty extracted text are moved to ``Others`` and hidden.

No résumé is uploaded, replaced, or deleted. The command is a dry run unless
``--apply`` is supplied and uses a checkpoint plus append-only change log.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select, text as sql_text
from sqlalchemy.exc import OperationalError

from .database import SessionLocal, engine
from .importers.parsing import extract_text_from_bytes, is_real_name
from .models import Profile
from .reclassify_other_profiles import Decision, classify_resume_role
from .services import storage


# Only legacy/unclassified rows are migration input. Rows already finalized as
# ``Others`` are excluded, which also makes a catch-up pass cheap while imports
# continue during a long production run.
_TARGET_CATEGORIES = ("Other",)
_MIN_ALPHA_CHARS = 80

# Damaged PDFs are handled through Outcome.document_valid; suppress pypdf's
# repetitive parser warnings so long production runs keep readable progress.
logging.getLogger("pypdf").setLevel(logging.CRITICAL)


@dataclass(frozen=True)
class Outcome:
    profile_id: str
    decision: Decision | None
    error: str | None
    text_chars: int
    name_valid: bool
    document_valid: bool


def _strict_name(first_name: str | None, last_name: str | None) -> bool:
    """Require two structurally plausible name parts for jobboard publication."""
    return bool(
        str(first_name or "").strip()
        and str(last_name or "").strip()
        and is_real_name(first_name, last_name)
    )


def _classify_one(row: dict) -> Outcome:
    name_valid = _strict_name(row.get("first_name"), row.get("last_name"))
    structured_decision = classify_resume_role(
        row.get("resume_sections") or "",
        profession_type=row.get("profession_type"),
        specialty=row.get("specialty"),
        headline=row.get("headline"),
    )
    if not row.get("resume_url"):
        return Outcome(
            row["profile_id"], structured_decision, "MissingResume", 0,
            name_valid, False,
        )

    try:
        key, is_local = storage.key_from_url(row["resume_url"])
        data = storage.download_bytes(key, prefer_local=is_local)
        full_text = extract_text_from_bytes(data, Path(key).name)
        alpha_chars = len(re.findall(r"[A-Za-z]", full_text or ""))
        decision = classify_resume_role(
            full_text,
            profession_type=row.get("profession_type"),
            specialty=row.get("specialty"),
            headline=row.get("headline"),
        ) or structured_decision
        return Outcome(
            row["profile_id"], decision, None, len(full_text or ""),
            name_valid, alpha_chars >= _MIN_ALPHA_CHARS,
        )
    except Exception as exc:  # noqa: BLE001 - isolate broken/unreadable files
        return Outcome(
            row["profile_id"], structured_decision, type(exc).__name__, 0,
            name_valid, False,
        )


def _new_values(row: dict, outcome: Outcome, *, assign_others: bool,
                hide_invalid: bool) -> dict | None:
    decision = outcome.decision
    if decision is None and not assign_others:
        return None

    exact_match = decision is not None
    category = decision.category if exact_match else "Others"
    profession = decision.profession if exact_match else row.get("profession_type")
    evidence = decision.evidence if exact_match else "No approved taxonomy match"
    listable = bool(row.get("is_listable"))
    if hide_invalid and listable and not (outcome.name_valid and outcome.document_valid):
        listable = False

    return {
        "profile_id": row["profile_id"],
        "category": category,
        "profession": profession,
        "replace_profession": exact_match,
        "is_listable": listable,
        "evidence": evidence,
        "error": outcome.error,
        "text_chars": outcome.text_chars,
        "name_valid": outcome.name_valid,
        "document_valid": outcome.document_valid,
    }


_UPDATE = sql_text("""
    UPDATE profiles
    SET provider_category = :category,
        profession_type = CASE
            WHEN :replace_profession THEN CAST(:profession AS VARCHAR)
            ELSE profession_type
        END,
        is_listable = :is_listable,
        completion_score = LEAST(
            100,
            completion_score + CASE
                WHEN :replace_profession
                 AND (profession_type IS NULL OR length(trim(profession_type)) = 0)
                THEN 10 ELSE 0
            END
        ),
        search_text = lower(concat_ws(' ',
            first_name, last_name, headline, bio, specialty,
            CASE WHEN :replace_profession THEN CAST(:profession AS VARCHAR)
                 ELSE profession_type END,
            city, state_code, american_board, CAST(:category AS VARCHAR)
        )),
        updated_at = now()
    WHERE profile_id = :profile_id
      AND (provider_category IN ('Other', 'Others') OR provider_category IS NULL)
""")


def _apply_batch(changes: list[dict], *, retries: int = 5) -> int:
    if not changes:
        return 0
    for attempt in range(retries):
        db = SessionLocal()
        try:
            updated = 0
            for change in changes:
                updated += db.execute(_UPDATE, change).rowcount or 0
            db.commit()
            return updated
        except OperationalError:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - connection may already be closed
                pass
            engine.dispose()
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
        finally:
            db.close()
    return 0


def _fetch_batch(stmt, *, retries: int = 5) -> list[dict]:
    for attempt in range(retries):
        try:
            with SessionLocal() as db:
                return [dict(row) for row in db.execute(stmt).mappings()]
        except OperationalError:
            engine.dispose()
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
    return []


def _append_log(path: Path, rows_by_id: dict[str, dict], changes: list[dict]) -> None:
    if not changes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    applied_at = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for change in changes:
            old = rows_by_id[change["profile_id"]]
            handle.write(json.dumps({
                "applied_at": applied_at,
                "profile_id": change["profile_id"],
                "old_category": old.get("provider_category"),
                "new_category": change["category"],
                "old_profession": old.get("profession_type"),
                "new_profession": change["profession"],
                "old_is_listable": old.get("is_listable"),
                "new_is_listable": change["is_listable"],
                "evidence": change["evidence"],
                "read_error": change["error"],
                "text_chars": change["text_chars"],
                "name_valid": change["name_valid"],
                "document_valid": change["document_valid"],
            }, sort_keys=True) + "\n")


def _resume_cursor(path: Path, *, restart: bool) -> str | None:
    if restart or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        value = payload.get("last_profile_id")
        return str(value) if value else None
    except (OSError, ValueError, TypeError):
        return None


def _write_checkpoint(path: Path, last_profile_id: str, stats: Counter,
                      *, complete: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_profile_id": last_profile_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "complete": complete,
        "run_stats": dict(stats),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize unclassified provider categories")
    parser.add_argument("--apply", action="store_true", help="commit changes; default is dry-run")
    parser.add_argument("--assign-others", action="store_true", help="assign unmatched profiles to Others")
    parser.add_argument("--hide-invalid", action="store_true", help="hide listable rows with invalid names/files")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--executor", choices=("process", "thread"), default="process")
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--only-listable", action="store_true")
    visibility.add_argument("--only-hidden", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--start-after", default="")
    parser.add_argument("--stop-at", default="", help="inclusive profile_id upper bound")
    parser.add_argument(
        "--changes", default="exports/provider_finalization_changes.jsonl",
    )
    parser.add_argument(
        "--checkpoint", default="exports/provider_finalization_checkpoint.json",
    )
    args = parser.parse_args()

    stats: Counter[str] = Counter()
    changes_path = Path(args.changes)
    checkpoint_path = Path(args.checkpoint)
    cursor = args.start_after.strip() or _resume_cursor(
        checkpoint_path, restart=args.restart or not args.apply
    )
    processed_scope = 0
    batch_size = max(1, args.batch_size)
    executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    print(
        f"Apply={args.apply} AssignOthers={args.assign_others} "
        f"HideInvalid={args.hide_invalid} Workers={args.workers} "
        f"BatchSize={batch_size} ResumeAfter={cursor or '-'}",
        flush=True,
    )

    with executor_cls(max_workers=max(1, args.workers)) as pool:
        while True:
            page_size = batch_size
            if args.limit:
                page_size = min(page_size, args.limit - processed_scope)
                if page_size <= 0:
                    break
            stmt = select(
                Profile.profile_id, Profile.first_name, Profile.last_name,
                Profile.resume_url, Profile.profession_type, Profile.specialty,
                Profile.headline, Profile.resume_sections, Profile.is_listable,
                Profile.provider_category, Profile.completion_score,
            ).where(or_(
                Profile.provider_category.in_(_TARGET_CATEGORIES),
                Profile.provider_category.is_(None),
            ))
            if args.only_listable:
                stmt = stmt.where(Profile.is_listable.is_(True))
            elif args.only_hidden:
                stmt = stmt.where(Profile.is_listable.is_(False))
            if cursor:
                stmt = stmt.where(Profile.profile_id > cursor)
            if args.stop_at.strip():
                stmt = stmt.where(Profile.profile_id <= args.stop_at.strip())
            stmt = stmt.order_by(Profile.profile_id).limit(page_size)
            batch = _fetch_batch(stmt)
            if not batch:
                break

            processed_scope += len(batch)
            batch_cursor = batch[-1]["profile_id"]
            outcomes = list(pool.map(_classify_one, batch))
            rows_by_id = {row["profile_id"]: row for row in batch}
            changes: list[dict] = []
            for outcome in outcomes:
                row = rows_by_id[outcome.profile_id]
                stats["processed"] += 1
                stats["input_listable" if row.get("is_listable") else "input_hidden"] += 1
                stats["name_valid" if outcome.name_valid else "name_invalid"] += 1
                stats["document_valid" if outcome.document_valid else "document_invalid"] += 1
                if outcome.name_valid and outcome.document_valid:
                    stats["eligible_quality"] += 1
                else:
                    stats["ineligible_quality"] += 1
                if outcome.error:
                    stats[f"read_error_{outcome.error}"] += 1
                if outcome.decision:
                    stats[f"classified_{outcome.decision.category}"] += 1
                    stats[f"profession_{outcome.decision.profession}"] += 1
                else:
                    stats["classified_Others"] += 1

                change = _new_values(
                    row, outcome,
                    assign_others=args.assign_others,
                    hide_invalid=args.hide_invalid,
                )
                if change is None:
                    continue
                old_profession = row.get("profession_type")
                if not change["replace_profession"]:
                    change["profession"] = old_profession
                changed = (
                    row.get("provider_category") != change["category"]
                    or old_profession != change["profession"]
                    or bool(row.get("is_listable")) != change["is_listable"]
                )
                if changed:
                    changes.append(change)
                    if row.get("is_listable") and not change["is_listable"]:
                        stats["newly_hidden_invalid"] += 1

            if args.apply:
                stats["updated"] += _apply_batch(changes)
                _append_log(changes_path, rows_by_id, changes)
                _write_checkpoint(checkpoint_path, batch_cursor, stats)
            cursor = batch_cursor
            print(
                f"Progress={processed_scope:,} Cursor={cursor} "
                f"changes={len(changes):,} four_categories="
                f"{sum(v for k, v in stats.items() if k.startswith('classified_') and k != 'classified_Others'):,} "
                f"others={stats['classified_Others']:,} invalid={stats['document_invalid']:,}",
                flush=True,
            )

    if args.apply and cursor:
        _write_checkpoint(checkpoint_path, cursor, stats, complete=True)
    print("Summary")
    for key, value in sorted(stats.items()):
        print(f"  {key}={value:,}")
    if not args.apply:
        print("Dry run only; no database rows changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
