"""Move evidence-backed nursing-unit profiles from Other(s) to Nursing.

The stored specialty is only used to select candidates. The final decision is
made from résumé content so legacy false ICU tags (for example, ``curriculum``)
cannot cause reclassification. Existing visibility is preserved and explicit
APP, physician, allied, or non-nursing profession evidence blocks the fallback.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select, text as sql_text
from sqlalchemy.exc import OperationalError

from .database import SessionLocal, engine
from .models import Profile
from .reclassify_other_profiles import Decision, _resume_text, classify_resume_role


_TARGET_SPECIALTIES = (
    "ICU", "ER", "PICU", "NICU", "Labor & Delivery", "Med-Surg",
    "Telemetry", "Oncology", "PACU", "OR", "Operating Room", "Dialysis",
)


@dataclass(frozen=True)
class Outcome:
    profile_id: str
    decision: Decision | None
    error: str | None


def _classify_one(row: dict) -> Outcome:
    structured = classify_resume_role(
        row.get("resume_sections") or "",
        profession_type=row.get("profession_type"),
        specialty=row.get("specialty"),
        headline=row.get("headline"),
    )
    if structured:
        if structured.category == "Nursing":
            return Outcome(row["profile_id"], structured, None)
        return Outcome(row["profile_id"], None, f"Conflicting{structured.category}")
    if not row.get("resume_url"):
        return Outcome(row["profile_id"], None, "MissingResume")
    try:
        full_text = _resume_text(row["resume_url"])
        decision = classify_resume_role(
            full_text,
            profession_type=row.get("profession_type"),
            specialty=row.get("specialty"),
            headline=row.get("headline"),
        )
        if decision and decision.category == "Nursing":
            return Outcome(row["profile_id"], decision, None)
        if decision:
            return Outcome(row["profile_id"], None, f"Conflicting{decision.category}")
        return Outcome(row["profile_id"], None, "NoNursingEvidence")
    except Exception as exc:  # noqa: BLE001 - isolate bad/unreadable objects
        return Outcome(row["profile_id"], None, type(exc).__name__)


_UPDATE = sql_text("""
    UPDATE profiles
    SET provider_category = 'Nursing',
        profession_type = :profession,
        completion_score = LEAST(
            100,
            completion_score + CASE
                WHEN profession_type IS NULL OR length(trim(profession_type)) = 0
                THEN 10 ELSE 0
            END
        ),
        search_text = lower(concat_ws(' ',
            first_name, last_name, headline, bio, specialty,
            CAST(:profession AS VARCHAR), city, state_code, american_board,
            'Nursing'
        )),
        updated_at = now()
    WHERE profile_id = :profile_id
      AND (provider_category IN ('Other', 'Others') OR provider_category IS NULL)
""")


def _fetch(stmt, *, retries: int = 5) -> list[dict]:
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


def _apply(outcomes: list[Outcome], *, retries: int = 5) -> int:
    decisions = [item for item in outcomes if item.decision]
    if not decisions:
        return 0
    for attempt in range(retries):
        db = SessionLocal()
        try:
            updated = 0
            for item in decisions:
                updated += db.execute(_UPDATE, {
                    "profile_id": item.profile_id,
                    "profession": item.decision.profession,
                }).rowcount or 0
            db.commit()
            return updated
        except OperationalError:
            db.rollback()
            engine.dispose()
            if attempt == retries - 1:
                raise
            time.sleep(min(2 ** attempt, 8))
        finally:
            db.close()
    return 0


def _append_log(path: Path, rows: dict[str, dict], outcomes: list[Outcome]) -> None:
    decisions = [item for item in outcomes if item.decision]
    if not decisions:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    applied_at = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for item in decisions:
            old = rows[item.profile_id]
            handle.write(json.dumps({
                "applied_at": applied_at,
                "profile_id": item.profile_id,
                "old_category": old.get("provider_category"),
                "new_category": "Nursing",
                "old_profession": old.get("profession_type"),
                "new_profession": item.decision.profession,
                "is_listable": bool(old.get("is_listable")),
                "specialty": old.get("specialty"),
                "evidence": item.decision.evidence,
            }, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reclassify nursing-unit profiles")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--executor", choices=("process", "thread"), default="process")
    parser.add_argument("--start-after", default="")
    parser.add_argument(
        "--changes", default="exports/nursing_specialty_reclassification.jsonl",
    )
    args = parser.parse_args()

    stats: Counter[str] = Counter()
    cursor = args.start_after.strip()
    processed = 0
    changes_path = Path(args.changes)
    executor_cls = ProcessPoolExecutor if args.executor == "process" else ThreadPoolExecutor
    with executor_cls(max_workers=max(1, args.workers)) as pool:
        while True:
            size = max(1, args.batch_size)
            if args.limit:
                size = min(size, args.limit - processed)
                if size <= 0:
                    break
            stmt = select(
                Profile.profile_id, Profile.resume_url, Profile.resume_sections,
                Profile.profession_type, Profile.specialty, Profile.headline,
                Profile.provider_category, Profile.is_listable,
            ).where(
                or_(
                    Profile.provider_category.in_(("Other", "Others")),
                    Profile.provider_category.is_(None),
                ),
                Profile.specialty.in_(_TARGET_SPECIALTIES),
            )
            if cursor:
                stmt = stmt.where(Profile.profile_id > cursor)
            stmt = stmt.order_by(Profile.profile_id).limit(size)
            batch = _fetch(stmt)
            if not batch:
                break
            cursor = batch[-1]["profile_id"]
            processed += len(batch)
            rows = {row["profile_id"]: row for row in batch}
            outcomes = list(pool.map(_classify_one, batch))
            accepted = [item for item in outcomes if item.decision]
            stats["processed"] += len(batch)
            stats["accepted"] += len(accepted)
            stats["accepted_listable"] += sum(
                bool(rows[item.profile_id]["is_listable"]) for item in accepted
            )
            for item in outcomes:
                if item.decision:
                    stats[f"evidence_{item.decision.evidence}"] += 1
                else:
                    stats[f"rejected_{item.error or 'Unknown'}"] += 1
            if args.apply:
                stats["updated"] += _apply(outcomes)
                _append_log(changes_path, rows, outcomes)
            print(
                f"Progress={processed:,} accepted={stats['accepted']:,} "
                f"cursor={cursor}",
                flush=True,
            )

    print("Summary")
    for key, value in sorted(stats.items()):
        print(f"  {key}={value:,}")
    if not args.apply:
        print("Dry run only; no database rows changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
